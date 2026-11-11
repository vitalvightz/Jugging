"""Optional exercise results on manual sessions: identity, lifecycle, library and boundaries."""
import copy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from api.services.exercise_library import exercise_library, exercise_performances
from api.services.training_history import training_history
from fightcamp.completed_workload import completed_workload
from tests.support import _build_client
from tests.test_exercise_library import entry as plan_log
from tests.test_external_training import adaptive_context, seed_manual

AUTH = {"Authorization": "Bearer athlete-token"}
OTHER = {"Authorization": "Bearer admin-token"}
BACK_SQUAT = "exercise_bank.json:1090492c-7713-4845-89fc-f311b9088445"
FRONT_SQUAT = "exercise_bank.json:21c9e4b8-181f-4f8d-a5d9-6ad56668c05c"
PLANK = "exercise_bank.json:6d878136-70a6-4eef-b160-71c1b2417309"
FUTURE = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def api():
    client, store, _ = _build_client()
    return client, store


def session(client, headers=AUTH, **changes):
    response = client.post("/api/training/sessions", headers=headers, json={
        "client_request_id": str(uuid4()), "activity": "strength", "duration_minutes": 60, "effort": "hard", **changes})
    assert response.status_code == 201, response.text
    return response.json()


def put(client, session_id, entry_id=None, headers=AUTH, **body):
    entry_id = entry_id or str(uuid4())
    payload = {"exercise": {"catalogue_id": BACK_SQUAT}, "format": "strength", **body}
    return client.put(f"/api/training/sessions/{session_id}/exercises/{entry_id}", headers=headers, json=payload)


def add(client, session_id, entry_id=None, **body):
    response = put(client, session_id, entry_id, **body)
    assert response.status_code == 200, response.text
    return response.json()


def clean(values):
    return {key: value for key, value in values.items() if value is not None}


def squat(sets=3, reps=5, value=80, unit="kg"):
    return {"sets": sets, "reps": reps, "load": {"value": value, "unit": unit}}


def details(client, session_id, headers=AUTH):
    return client.get(f"/api/training/history/external/{session_id}", headers=headers)


def library(store, athlete="athlete-1"):
    return {item.id: item for item in exercise_library(store, athlete, now=FUTURE).exercises}


def performances(store, identity, **kwargs):
    return exercise_performances(store, "athlete-1", identity, as_of=FUTURE.isoformat(), health_consent_granted=True, **kwargs)


def history_rows(client):
    return client.get("/api/training/history", headers=AUTH).json()["sessions"]


def test_session_saves_without_exercises_and_stays_complete(api):
    client, store = api
    row = session(client)
    assert row["source"] == "external"
    assert not store.manual_exercise_entries
    result = details(client, row["id"]).json()
    assert result["blocks"] == [] and result["session"]["exercise_count"] == 0
    assert history_rows(client)[0]["exercise_count"] == 0
    assert client.get(f"/api/training/sessions/{row['id']}/exercises", headers=AUTH).json() == {"entries": []}


def test_exercise_added_right_after_saving_appears_in_details_history_and_library(api):
    client, store = api
    row = session(client)
    saved = add(client, row["id"], actual=squat())
    assert saved["name"] == "Back Squat" and saved["exercise_id"] == "key:back-squat"
    assert saved["training_day"] == row["training_day"] and saved["external_session_id"] == row["id"]
    blocks = details(client, row["id"]).json()["blocks"]
    assert [(block["name"], block["source"], block["format"], block["prescribed"]) for block in blocks] == [
        ("Back Squat", "manual", "strength", {})]
    assert clean(blocks[0]["actual"]) == {"sets": 3, "reps": 5, "load": {"value": 80.0, "unit": "kg"}}
    assert history_rows(client)[0]["exercise_count"] == 1
    item = library(store)["key:back-squat"]
    assert item.latest_source == "manual" and item.log_format == "strength"
    assert [(load.value, load.unit, load.basis) for load in item.best_loads] == [(80, "kg", "reported")]


def test_exercises_added_and_edited_later_from_history(api):
    client, store = api
    row = session(client, training_day="2026-10-01")
    entry_id = str(uuid4())
    add(client, row["id"], entry_id, actual=squat(value=70))
    edited = add(client, row["id"], entry_id, actual=squat(value=75), notes="Belt on", revision=1)
    assert edited["actual"]["load"]["value"] == 75 and edited["notes"] == "Belt on"
    assert edited["training_day"] == "2026-10-01"
    listed = client.get(f"/api/training/sessions/{row['id']}/exercises", headers=AUTH).json()["entries"]
    assert [item["entry_id"] for item in listed] == [entry_id]
    assert len(store.manual_exercise_entries) == 1


def test_new_custom_name_alone_creates_one_private_identity_and_records_no_values(api):
    client, store = api
    row = session(client)
    first = add(client, row["id"], exercise={"name": "  Landmine   rotation "}, format="bodyweight")
    second = add(client, session(client)["id"], exercise={"name": "landmine ROTATION"}, format="bodyweight")
    assert first["name"] == "Landmine rotation" and clean(first["actual"]) == {}
    assert first["exercise_id"] == second["exercise_id"] and first["exercise_id"].startswith("custom:")
    assert len(store.personal_exercises) == 1
    # A qualifier is a different movement, never a fuzzy match.
    third = add(client, row["id"], exercise={"name": "Half-kneeling landmine rotation"}, format="bodyweight")
    assert third["exercise_id"] != first["exercise_id"]
    item = library(store)[first["exercise_id"]]
    assert item.identity_kind == "personal" and item.best_loads == [] and item.latest_values.model_dump(exclude_none=True) == {}
    # Reselecting by its private ID keeps the same identity.
    reuse = add(client, row["id"], exercise={"personal_exercise_id": first["exercise_id"][7:]}, format="bodyweight")
    assert reuse["exercise_id"] == first["exercise_id"]
    # Nothing reaches the community library.
    assert not getattr(store, "community_exercises", None)


def test_catalogue_and_prescribed_logs_share_exact_keys_and_variations_stay_apart(api):
    client, store = api
    store.exercise_logs.append(plan_log(1, actual={"load": {"value": 100, "unit": "kg"}}))
    row = session(client)
    add(client, row["id"], actual=squat(value=80))
    add(client, row["id"], exercise={"catalogue_id": FRONT_SQUAT}, actual=squat(value=60))
    add(client, row["id"], exercise={"name": "Back squat"}, actual=squat(value=200))
    items = library(store)
    assert items["key:back-squat"].best_loads[0].value == 100
    assert items["key:front-squat"].best_loads[0].value == 60
    custom = next(item for item in items.values() if item.identity_kind == "personal")
    assert custom.best_loads[0].value == 200
    history = performances(store, "key:back-squat").performances
    assert [item.block.source for item in history] == ["manual", "plan"]
    # A key already in the athlete's records can be chosen again directly.
    assert add(client, row["id"], exercise={"exercise_key": "back-squat"})["name"] == "Back Squat"
    assert put(client, row["id"], exercise={"exercise_key": "made-up-movement"}).status_code == 422
    assert put(client, row["id"], exercise={"catalogue_id": "missing"}).status_code == 422


def test_multiple_exercises_keep_order_and_same_day_sessions_stay_distinct(api):
    client, _ = api
    first, second = session(client), session(client, activity="conditioning")
    for catalogue_id in (BACK_SQUAT, FRONT_SQUAT, PLANK):
        add(client, first["id"], exercise={"catalogue_id": catalogue_id},
            format="isometric" if catalogue_id == PLANK else "strength")
    add(client, second["id"], exercise={"name": "Assault bike"}, format="intervals",
        actual={"rounds": 10, "work": {"value": 20, "unit": "seconds"}, "rest": {"value": 40, "unit": "seconds"}})
    assert [block["name"] for block in details(client, first["id"]).json()["blocks"]] == ["Back Squat", "Front Squat", "Plank"]
    assert [block["name"] for block in details(client, second["id"]).json()["blocks"]] == ["Assault bike"]
    counts = {row["id"]: row["exercise_count"] for row in history_rows(client)}
    assert counts == {first["id"]: 3, second["id"]: 1}


@pytest.mark.parametrize("fmt,actual", [
    ("strength", {"sets": 3, "reps": 5, "load": {"value": 80, "unit": "kg"}, "rest": {"value": 2, "unit": "minutes"}, "effort": {"value": 8.5}}),
    ("bodyweight", {"sets": 3, "reps": 12}),
    ("bodyweight", {"sets": 3, "reps": 8, "load": {"value": 10, "unit": "kg"}}),
    ("isometric", {"sets": 3, "duration": {"value": 45, "unit": "seconds"}}),
    ("intervals", {"rounds": 6, "work": {"value": 3, "unit": "minutes"}, "rest": {"value": 60, "unit": "seconds"}}),
    ("distance", {"distance": {"value": 5, "unit": "km"}, "duration": {"value": 28, "unit": "minutes"}}),
    ("strength", {}), ("strength", {"sets": 3}), ("intervals", {"rest": {"value": 0, "unit": "seconds"}}),
])
def test_each_format_records_only_what_was_entered(api, fmt, actual):
    client, _ = api
    saved = add(client, session(client)["id"], format=fmt, actual=actual)
    assert saved["format"] == fmt
    assert clean(saved["actual"]) == {
        key: (value if not isinstance(value, dict) else {**value, "value": float(value["value"]), **({"method": "RPE"} if key == "effort" else {})})
        for key, value in actual.items()}


@pytest.mark.parametrize("fmt,actual", [
    ("strength", {"sets": 0}), ("strength", {"reps": 0}), ("strength", {"load": {"value": 0, "unit": "kg"}}),
    ("strength", {"load": {"value": 1001, "unit": "kg"}}), ("strength", {"load": {"value": 80.123, "unit": "kg"}}),
    ("strength", {"load": {"value": 80, "unit": "stone"}}), ("strength", {"load": {"value": 80, "unit": "percent"}}),
    ("strength", {"sets": 51}), ("strength", {"duration": {"value": 30, "unit": "seconds"}}),
    ("strength", {"effort": {"value": 7.3}}), ("strength", {"effort": {"value": 11}}),
    ("isometric", {"reps": 5}), ("intervals", {"work": {"value": 61, "unit": "minutes"}}),
    ("distance", {"distance": {"value": 300, "unit": "km"}}), ("distance", {"distance": {"value": 0, "unit": "meters"}}),
    ("strength", {"sets": "3"}), ("strength", {"sets": True}), ("strength", {"sets": 2.5}),
    ("strength", {"load": {"value": "80", "unit": "kg"}}), ("cardio", {}),
])
def test_unrealistic_or_mismatched_values_are_refused(api, fmt, actual):
    client, store = api
    assert put(client, session(client)["id"], format=fmt, actual=actual).status_code == 422
    assert not store.manual_exercise_entries


@pytest.mark.parametrize("exercise", [{}, {"name": ""}, {"name": "x" * 101},
    {"name": "Row", "catalogue_id": BACK_SQUAT}, {"exercise_key": "Back Squat"}])
def test_exactly_one_valid_exercise_identity_is_required(api, exercise):
    client, _ = api
    assert put(client, session(client)["id"], exercise=exercise).status_code == 422


def test_kg_and_lb_are_separate_bests_and_a_deleted_heavier_entry_never_counts(api):
    client, store = api
    row = session(client)
    add(client, row["id"], actual=squat(value=80))
    add(client, row["id"], actual=squat(value=90))
    heavy = str(uuid4())
    add(client, row["id"], heavy, actual=squat(value=120))
    add(client, row["id"], actual=squat(value=200, unit="lb"))
    assert client.delete(f"/api/training/sessions/{row['id']}/exercises/{heavy}", headers=AUTH).status_code == 204
    bests = library(store)["key:back-squat"].best_loads
    assert [(load.value, load.unit) for load in bests] == [(90, "kg"), (200, "lb")]
    values = [item.performed_values.load.value for item in performances(store, "key:back-squat").performances]
    assert sorted(values) == [80, 90, 200]


def test_missing_measurements_are_never_zero_or_a_load(api):
    client, store = api
    row = session(client)
    add(client, row["id"], actual={"sets": 3, "reps": 5})
    item = library(store)["key:back-squat"]
    assert item.best_loads == [] and item.latest_values.load is None
    record = performances(store, "key:back-squat").performances[0]
    assert record.performed_values.load is None and record.block.prescribed == {}


def test_progress_history_pages_across_sources_without_duplicates(api):
    client, store = api
    store.exercise_logs.extend(plan_log(i, training_day=f"2026-09-{i:02d}") for i in range(1, 8))
    for day in ("2026-09-03", "2026-09-05", "2026-10-02"):
        add(client, session(client, training_day=day)["id"], actual=squat())
    seen, cursor = [], None
    while True:
        page = performances(store, "key:back-squat", limit=3, cursor=cursor)
        seen += [(item.training_day, item.id) for item in page.performances]
        cursor = page.next_cursor
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 10
    assert seen == sorted(seen, reverse=True)
    custom = add(client, session(client)["id"], exercise={"name": "Sandbag carry"}, format="distance")
    assert [item.block.name for item in performances(store, custom["exercise_id"]).performances] == ["Sandbag carry"]


def test_parent_edit_keeps_entries_and_parent_delete_hides_them_everywhere(api):
    client, store = api
    row = session(client)
    entry_id = str(uuid4())
    add(client, row["id"], entry_id, actual=squat(value=150))
    assert client.patch(f"/api/training/sessions/{row['id']}", headers=AUTH,
        json={"activity": "conditioning", "duration_minutes": 50, "effort": "moderate"}).status_code == 200
    assert len(details(client, row["id"]).json()["blocks"]) == 1
    assert client.delete(f"/api/training/sessions/{row['id']}", headers=AUTH).status_code == 204
    assert details(client, row["id"]).status_code == 404
    assert client.get(f"/api/training/sessions/{row['id']}/exercises", headers=AUTH).status_code == 404
    assert "key:back-squat" not in library(store)
    with pytest.raises(Exception):
        performances(store, "key:back-squat")
    assert all(entry["deleted_at"] for entry in store.manual_exercise_entries)
    # No write can reach entries under a deleted session.
    assert put(client, row["id"], entry_id, actual=squat()).status_code == 404
    assert put(client, row["id"], actual=squat()).status_code == 404


def test_parent_date_change_moves_its_entries(api):
    client, store = api
    row = session(client, training_day="2026-10-01")
    add(client, row["id"], actual=squat())
    store.external_training_sessions[row["id"]]["training_day"] = "2026-09-30"
    store.sync_manual_exercise_parent(row["id"])
    assert library(store)["key:back-squat"].latest_day == "2026-09-30"
    assert details(client, row["id"]).json()["session"]["training_day"] == "2026-09-30"


def test_delayed_retry_of_an_old_save_never_overwrites_a_later_correction(api):
    client, store = api
    row = session(client)
    entry_id = str(uuid4())
    original = {"exercise": {"catalogue_id": BACK_SQUAT}, "format": "strength", "actual": squat(value=80), "revision": 0}
    # 1. Saves Back Squat at 80 kg.
    first = client.put(f"/api/training/sessions/{row['id']}/exercises/{entry_id}", headers=AUTH, json=original)
    assert first.status_code == 200 and first.json()["revision"] == 1
    # 2. Corrects it to 90 kg, based on revision 1.
    corrected = add(client, row["id"], entry_id, actual=squat(value=90), revision=1)
    assert corrected["revision"] == 2
    # 3. The original 80 kg request arrives late.
    late = client.put(f"/api/training/sessions/{row['id']}/exercises/{entry_id}", headers=AUTH, json=original)
    assert late.status_code == 409
    stored = client.get(f"/api/training/sessions/{row['id']}/exercises", headers=AUTH).json()["entries"]
    assert [(item["actual"]["load"]["value"], item["revision"]) for item in stored] == [(90, 2)]
    assert library(store)["key:back-squat"].best_loads[0].value == 90
    assert performances(store, "key:back-squat").performances[0].performed_values.load.value == 90


def test_replays_of_the_latest_save_succeed_but_stale_or_skipped_revisions_are_refused(api):
    client, store = api
    row = session(client)
    entry_id = str(uuid4())
    add(client, row["id"], entry_id, actual=squat(value=80))
    corrected = add(client, row["id"], entry_id, actual=squat(value=90), revision=1)
    # A lost response for the correction is retried with the same body: same row, no extra revision.
    assert add(client, row["id"], entry_id, actual=squat(value=90), revision=1) == corrected
    # An edit based on an old revision with new values is a conflict, not a silent overwrite.
    assert put(client, row["id"], entry_id, actual=squat(value=95), revision=1).status_code == 409
    assert put(client, row["id"], entry_id, actual=squat(value=95), revision=5).status_code == 409
    # Editing an entry that doesn't exist yet is not a create.
    assert put(client, row["id"], str(uuid4()), revision=3).status_code == 404
    assert [entry["actual"]["load"]["value"] for entry in store.manual_exercise_entries] == [90]
    assert put(client, row["id"], entry_id, actual=squat(value=95), revision=2).json()["revision"] == 3


def test_a_correction_racing_with_another_write_cannot_overwrite_it(api):
    client, store = api
    row = session(client)
    entry_id = str(uuid4())
    add(client, row["id"], entry_id, actual=squat(value=80))
    original_update = store.update_manual_exercise

    def concurrent_win(athlete_id, entry, fields, *, revision):
        # Another device's 100 kg correction commits between the read and the write.
        original_update(athlete_id, entry, {**fields, "actual": squat(value=100)}, revision=revision)
        return original_update(athlete_id, entry, fields, revision=revision)

    store.update_manual_exercise = concurrent_win
    assert put(client, row["id"], entry_id, actual=squat(value=90), revision=1).status_code == 409
    assert store.manual_exercise_entries[0]["actual"]["load"]["value"] == 100


def test_retries_are_idempotent_and_tombstones_block_resurrection(api):
    client, store = api
    row, other = session(client), session(client)
    entry_id = str(uuid4())
    first = add(client, row["id"], entry_id, actual=squat())
    assert add(client, row["id"], entry_id, actual=squat())["id"] == first["id"]
    assert len(store.manual_exercise_entries) == 1
    assert put(client, other["id"], entry_id, actual=squat()).status_code == 409
    path = f"/api/training/sessions/{row['id']}/exercises/{entry_id}"
    assert client.delete(path, headers=AUTH).status_code == 204
    assert client.delete(path, headers=AUTH).status_code == 204
    assert put(client, row["id"], entry_id, actual=squat()).status_code == 410
    assert client.delete(f"/api/training/sessions/{other['id']}/exercises/{entry_id}", headers=AUTH).status_code == 404
    assert client.delete(f"/api/training/sessions/{row['id']}/exercises/{uuid4()}", headers=AUTH).status_code == 404
    assert put(client, row["id"], "not-a-uuid").status_code == 422


def test_session_entry_limit_is_enforced_server_side(api):
    client, _ = api
    row = session(client)
    for _ in range(30):
        add(client, row["id"], exercise={"name": "Curl"}, format="strength")
    assert put(client, row["id"]).status_code == 422


def test_repeating_a_session_never_copies_previous_actuals(api):
    client, store = api
    old = session(client, training_day="2026-10-01")
    add(client, old["id"], actual=squat(value=100))
    repeat = session(client)
    assert details(client, repeat["id"]).json()["blocks"] == []
    assert len(store.manual_exercise_entries) == 1
    # Reusing the name records a new occurrence with only what is entered now.
    fresh = add(client, repeat["id"], exercise={"exercise_key": "back-squat"})
    assert clean(fresh["actual"]) == {} and fresh["id"] != store.manual_exercise_entries[0]["id"]


def test_consent_withdrawal_blocks_writes_and_hides_notes(api):
    client, store = api
    row = session(client)
    entry_id = str(uuid4())
    add(client, row["id"], entry_id, notes="Knee felt fine")
    store.profiles["athlete-1"]["health_consent_withdrawn_at"] = "2026-10-10T12:00:00Z"
    store.profiles["athlete-1"]["health_data_consent"] = False
    assert put(client, row["id"]).status_code == 403
    assert put(client, row["id"], entry_id).status_code == 403
    listed = client.get(f"/api/training/sessions/{row['id']}/exercises", headers=AUTH).json()["entries"]
    assert listed[0]["notes"] is None
    assert details(client, row["id"]).json()["blocks"][0]["notes"] is None
    assert client.delete(f"/api/training/sessions/{row['id']}/exercises/{entry_id}", headers=AUTH).status_code == 204


def test_athletes_are_isolated(api):
    client, store = api
    row = session(client)
    entry_id = str(uuid4())
    mine = add(client, row["id"], entry_id, exercise={"name": "Secret drill"}, format="bodyweight")
    other = session(client, OTHER)
    assert client.get(f"/api/training/sessions/{row['id']}/exercises", headers=OTHER).status_code == 404
    assert put(client, row["id"], headers=OTHER).status_code == 404
    assert put(client, other["id"], entry_id, headers=OTHER).status_code == 200
    assert client.delete(f"/api/training/sessions/{row['id']}/exercises/{entry_id}", headers=OTHER).status_code == 404
    stolen = put(client, other["id"], headers=OTHER, exercise={"personal_exercise_id": mine["exercise_id"][7:]})
    assert stolen.status_code == 422
    assert mine["exercise_id"] not in library(store, "admin-1")
    assert library(store)[mine["exercise_id"]].name == "Secret drill"
    assert {entry["athlete_id"] for entry in store.manual_exercise_entries} == {"athlete-1", "admin-1"}
    assert len([e for e in store.manual_exercise_entries if e["athlete_id"] == "athlete-1"]) == 1


def test_exercise_entries_add_no_workload_xp_rehab_or_sparring_evidence(api):
    client, store = api
    row = seed_manual(store)
    before_context = adaptive_context(store)
    before_rows = training_history(store, "athlete-1")
    before_workload = completed_workload(before_rows, row["training_day"])
    before = copy.deepcopy((store.xp_awards, store.rehab_exposures, store.injury_flags, store.sparring_logs,
                            store.session_completions, store.exercise_logs))
    for _ in range(3):
        add(client, row["id"], actual=squat())
    assert training_history(store, "athlete-1") == before_rows
    assert completed_workload(training_history(store, "athlete-1"), row["training_day"]) == before_workload
    assert adaptive_context(store) == before_context
    assert before == (store.xp_awards, store.rehab_exposures, store.injury_flags, store.sparring_logs,
                      store.session_completions, store.exercise_logs)


def test_library_search_for_logging_suggests_a_layout_only_from_catalogue_evidence(api):
    client, _ = api
    result = client.get("/api/training/library?q=back%20squat&for_log=true&limit=5", headers=AUTH).json()
    squat_entry = next(entry for entry in result["entries"] if entry["catalogue_id"] == BACK_SQUAT)
    assert squat_entry["log_format"] == "strength"
    plank = client.get("/api/training/library?q=plank&for_log=true", headers=AUTH).json()["entries"]
    assert next(entry for entry in plank if entry["catalogue_id"] == PLANK)["log_format"] == "isometric"
    plain = client.get("/api/training/library?q=back%20squat&limit=5", headers=AUTH).json()["entries"]
    assert all(entry["log_format"] is None for entry in plain)


def test_varied_sets_round_trip_through_manual_history_and_best_load(api):
    client, store = api
    row = session(client)
    sets = [{"reps": 8, "load": {"value": 25, "unit": "kg"}},
            {"reps": 6, "load": {"value": 30, "unit": "kg"}}]
    saved = add(client, row["id"], actual={"set_entries": sets})
    assert saved["actual"]["set_entries"] == [{**entry, "rpe": None, "set_number": None} for entry in sets]
    assert saved["actual"]["sets"] == 2
    assert saved["actual"]["reps"] is None and saved["actual"]["load"] is None
    assert details(client, row["id"]).json()["blocks"][0]["actual"]["set_entries"] == [{**entry, "rpe": None, "set_number": None} for entry in sets]
    result = library(store)["key:back-squat"]
    assert result.best_loads[0].value == 30
    assert result.latest_values.set_entries[0].load.value == 25
    assert result.latest_values.load is None


@pytest.mark.parametrize("sets", [[], [{"reps": True}], [{"reps": "8"}], [{"reps": 1.5}],
    [{"load": {"value": "25", "unit": "kg"}}], [{"load": {"value": 25, "unit": "percent"}}],
    [{"load": {"value": 25, "unit": "kg"}}, {"load": {"value": 55, "unit": "lb"}}]])
def test_invalid_set_entries_are_rejected(api, sets):
    client, _ = api
    row = session(client)
    assert put(client, row["id"], actual={"set_entries": sets}).status_code == 422


def test_uniform_sets_have_truthful_legacy_summary_and_other_formats_refuse_rows(api):
    client, _ = api
    row = session(client)
    sets = [{"reps": 8, "load": {"value": 25, "unit": "kg"}}] * 3
    saved = add(client, row["id"], actual={"set_entries": sets, "sets": 50, "reps": 99})
    assert saved["actual"]["sets"] == 3 and saved["actual"]["reps"] == 8
    assert saved["actual"]["load"]["value"] == 25
    assert put(client, row["id"], format="intervals", actual={"set_entries": sets}).status_code == 422


def test_per_set_rpe_is_preserved_in_history_and_common_effort_is_truthful(api):
    client, _ = api
    row = session(client)
    sets = [{"reps": 5, "load": {"value": 100, "unit": "kg"}, "rpe": 7},
            {"reps": 5, "load": {"value": 105, "unit": "kg"}, "rpe": 8.5}]
    saved = add(client, row["id"], actual={"set_entries": sets})
    assert saved["actual"]["set_entries"] == [{**entry, "set_number": None} for entry in sets]
    assert saved["actual"]["effort"] is None
    assert details(client, row["id"]).json()["blocks"][0]["actual"]["set_entries"] == [{**entry, "set_number": None} for entry in sets]
    uniform = add(client, row["id"], actual={"set_entries": [sets[0], sets[0]]})
    assert uniform["actual"]["effort"] == {"method": "RPE", "value": 7}


@pytest.mark.parametrize("rpe", [0, 11, 7.3, True, "7"])
def test_invalid_per_set_rpe_is_rejected(api, rpe):
    client, _ = api
    row = session(client)
    assert put(client, row["id"], actual={"set_entries": [{"reps": 5, "rpe": rpe}]}).status_code == 422


def test_manual_sparse_set_results_preserve_positions_and_fit_the_bounded_storage(api):
    import json

    client, _ = api
    row = session(client)
    sets = [{"set_number": number, "reps": 1000, "load": {"value": 1000, "unit": "kg"}, "rpe": 10}
            for number in range(1, 51) if number != 2]
    saved = add(client, row["id"], actual={"sets": 50, "set_entries": sets})
    assert saved["actual"]["sets"] == 50
    assert [entry["set_number"] for entry in saved["actual"]["set_entries"]] == [entry["set_number"] for entry in sets]
    assert saved["actual"]["load"] is None and saved["actual"]["reps"] is None
    assert len(json.dumps(saved["actual"]).encode()) < 8192


def test_manual_completion_supplies_actual_progression_evidence_without_a_historical_target(api):
    client, store = api
    parent = session(client)
    add(client, parent["id"], actual={**squat(3, 10, 80), "effort": {"method": "RPE", "value": 6}})
    response = client.get("/api/history/exercise-library", headers=AUTH)
    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["exercises"] if row["id"] == "key:back-squat")
    assert item["latest_performance"]["prescribed"] == {"display_name": "Back Squat"}
    assert item["latest_performance"]["actual"]["sets"] == 3
    assert item["latest_performance"]["actual"]["effort"]["value"] == 6
    assert item["progression_hold"] is False
    assert item["allow_progression"] is True


@pytest.mark.parametrize("name,format,held", [("Bench Press", "strength", False), ("My Push-Up", "bodyweight", False),
    ("Single-Leg Forward Hops", "strength", True), ("Unclassified movement", "intervals", None)])
def test_private_exercise_uses_recorded_type_without_extending_the_bank(api, name, format, held):
    client, _ = api
    parent = session(client)
    saved = add(client, parent["id"], exercise={"name": name}, format=format,
                actual={"sets": 3, "reps": 10, "load": {"value": 80, "unit": "kg"}, "effort": {"method": "RPE", "value": 6}} if format == "strength" else {})
    item = next(row for row in client.get("/api/history/exercise-library", headers=AUTH).json()["exercises"] if row["id"] == saved["exercise_id"])
    assert item["progression_hold"] is held
    assert item["bodyweight_progression"] is (True if format == "bodyweight" else None)
    assert item["latest_performance"]["prescribed"] == {"display_name": name}
