"""Custom drafts: owner CRUD, exact catalogue identity, order and dose bounds."""
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from api.services.training_library import cache
from api.services.custom_plans import _dose_text
from api.contracts.custom_plans import CustomDose
from api.services.exercise_media import reset_media_index_cache
from api.store import SupabaseAppStore
from tests.support import _build_client

AUTH = {"Authorization": "Bearer athlete-token"}
OTHER = {"Authorization": "Bearer admin-token"}
URL = "/api/custom-plans"


@pytest.mark.parametrize("rpe, expected", [(7.0, "RPE 7"), (7.5, "RPE 7.5")])
def test_custom_plan_rpe_display(rpe, expected):
    dose = CustomDose(mode="reps", sets=3, reps=8, rpe=rpe)
    assert _dose_text(dose.model_dump()) == f"3 × 8 · {expected}"


def test_source_suggestion_uses_athlete_labels(api, identities, monkeypatch):
    client, _ = api
    identity = identities["training"][0]
    record = copy.deepcopy(cache.get().by_id[identity])
    record["entry"]["raw"] = {
        "duration": "2x120s with 60s rest", "rounds": 2, "work_sec": 120,
        "rest_sec": 60, "total_minutes": 5.0, "rpe": 7.0,
    }
    monkeypatch.setattr(cache, "get", lambda: SimpleNamespace(by_id={identity: record}))
    response = client.get(
        f"{URL}/source-suggestion", params={"catalogue_id": identity}, headers=AUTH,
    )
    assert response.status_code == 200, response.text
    suggestion = response.json()
    assert suggestion["original"] == (
        "2x120s with 60s rest · Rounds: 2 · Work: 120 sec · Rest: 60 sec · Total time: 5 min · RPE: 7"
    )
    assert suggestion["dose"]["prescription"] == suggestion["original"]
    assert "work_sec" not in suggestion["original"]
    assert "rest_sec" not in suggestion["original"]
    assert "total_minutes" not in suggestion["original"]


@pytest.fixture(scope="module")
def identities():
    records = cache.get().records
    return {category: [record["entry"]["catalogue_id"] for record in records
                       if record["summary"]["category"] == category][:2]
            for category in ("training", "rehab", "learn")}


@pytest.fixture
def api():
    client, store, _ = _build_client()
    reset_media_index_cache()
    yield client, store
    reset_media_index_cache()


def payload(identities):
    return {"name": "My weekly plan", "sessions": [
        {"id": str(uuid4()), "name": "Strength", "weekday": "Monday", "exercises": [
            {"id": str(uuid4()), "catalogue_id": identity, "dose": {"mode": "reps", "sets": 3, "reps": 5, "rest_seconds": 90}}
            for identity in identities["training"]]},
        {"id": str(uuid4()), "name": "Conditioning", "weekday": "Tuesday", "exercises": []},
    ]}


def test_owner_crud_preserves_order_identity_and_generated_state(api, identities):
    client, store = api
    before = copy.deepcopy(store.plans)
    body = payload(identities)
    key = cache.get().by_id[identities["training"][0]]["entry"]["exercise_key"]
    store.exercise_media[key] = {"exercise_key": key, "video_id": "abcdefghijk", "made_for_kids": False, "status": "ok", "source": "curated"}
    created = client.post(URL, headers=AUTH, json=body)
    assert created.status_code == 201, created.text
    plan = created.json()
    path = f"{URL}/{plan['id']}"
    assert plan["kind"] == "self_directed" and plan["incomplete"]
    assert plan["sessions"][0]["exercises"][0]["snapshot"]["media"]["video_id"] == "abcdefghijk"
    for expected, exercise in zip(body["sessions"][0]["exercises"], plan["sessions"][0]["exercises"]):
        assert exercise["catalogue_id"] == expected["catalogue_id"] == exercise["snapshot"]["catalogue_id"]
        assert exercise["snapshot"]["source_revision"] and exercise["snapshot"]["row_revision"]
        assert exercise["snapshot"]["prescription_allowed"] is False
        preview = client.get("/api/training/library/entry", headers=AUTH, params={"catalogue_id": exercise["catalogue_id"]}).json()
        assert exercise["snapshot"]["exercise_key"] == preview["exercise_key"]
        assert exercise["snapshot"]["media"] == preview["media"]
    assert client.get(path, headers=AUTH).json() == plan
    assert client.get(URL, headers=AUTH).json() == [plan]
    assert client.get(URL, headers=OTHER).json() == []
    assert client.get(path, headers=OTHER).status_code == 404
    assert client.put(path, headers=OTHER, json=body).status_code == 404
    assert client.delete(path, headers=OTHER).status_code == 404
    body["name"] = "Edited weekly plan"
    body["sessions"].reverse()
    body["sessions"][1]["exercises"].reverse()
    body["sessions"][1]["exercises"][0]["dose"] = {"mode": "duration", "duration_seconds": 30, "rest_seconds": 0}
    edited = client.put(path, headers=AUTH, json=body)
    assert edited.status_code == 200, edited.text
    reopened = client.get(path, headers=AUTH).json()
    assert reopened["name"] == body["name"]
    assert [session["id"] for session in reopened["sessions"]] == [session["id"] for session in reversed(body["sessions"])]
    assert [exercise["id"] for exercise in reopened["sessions"][0]["exercises"]] == [exercise["id"] for exercise in body["sessions"][1]["exercises"]]
    assert reopened["sessions"][0]["exercises"][0]["dose"]["duration_seconds"] == 30
    assert client.delete(path, headers=AUTH).status_code == 204
    assert client.get(path, headers=AUTH).status_code == 404
    assert client.get(URL, headers=AUTH).json() == []
    assert store.plans == before
    assert not store.session_completions and not store.external_training_sessions


@pytest.mark.parametrize("category", ["rehab", "learn", "missing"])
def test_only_exact_training_identities_are_addable(api, identities, category):
    client, store = api
    body = payload(identities)
    body["sessions"][0]["exercises"][0]["catalogue_id"] = identities[category][0] if category != "missing" else "made-up"
    assert client.post(URL, headers=AUTH, json=body).status_code == 422
    assert not store.custom_plans


@pytest.mark.parametrize("dose", [
    {"sets": 0}, {"sets": True}, {"reps": -1}, {"reps": 2.5}, {"reps": "5"},
    {"duration_seconds": 30}, {"rest_seconds": -1}, {"rest_seconds": 3601},
    {"mode": "duration", "duration_seconds": 0}, {"mode": "duration", "reps": 5},
    {"mode": "minutes"}, {"load_or_intensity": "x" * 81},
])
def test_invalid_doses_rejected_without_writes(api, identities, dose):
    client, store = api
    body = payload(identities)
    body["sessions"][0]["exercises"][0]["dose"] = dose
    assert client.post(URL, headers=AUTH, json=body).status_code == 422
    assert not store.custom_plans


def test_incomplete_drafts_and_unassigned_days_persist(api, identities):
    client, _ = api
    body = payload(identities)
    body["sessions"] = body["sessions"][:1]
    body["sessions"][0]["weekday"] = None
    exercise = body["sessions"][0]["exercises"][0]
    exercise["dose"] = {}
    saved = client.post(URL, headers=AUTH, json=body).json()
    assert saved["incomplete"] and saved["sessions"][0]["exercises"][0]["incomplete"]
    assert saved["sessions"][0]["exercises"][0]["dose"]["reps"] is None
    assert saved["sessions"][0]["weekday"] is None
    exercise["dose"] = {"mode": "duration", "duration_seconds": 45}
    complete = client.put(f"{URL}/{saved['id']}", headers=AUTH, json=body).json()
    assert complete["incomplete"]  # A finished dose still needs a calendar day.
    body["sessions"][0]["weekday"] = "Monday"
    complete = client.put(f"{URL}/{saved['id']}", headers=AUTH, json=body).json()
    assert not complete["incomplete"]


def test_cannot_forge_owner_snapshot_or_execution_fields(api, identities):
    client, store = api
    for field in ("athlete_id", "kind", "plan_id", "active"):
        assert client.post(URL, headers=AUTH, json={**payload(identities), field: "forged"}).status_code == 422
    body = payload(identities)
    body["sessions"][0]["exercises"][0]["snapshot"] = {"name": "forged"}
    assert client.post(URL, headers=AUTH, json=body).status_code == 422
    body = payload(identities)
    body["sessions"][0]["exercises"][1]["id"] = body["sessions"][0]["exercises"][0]["id"]
    assert client.post(URL, headers=AUTH, json=body).status_code == 422
    assert not store.custom_plans


def test_all_routes_require_authentication(api, identities):
    client, _ = api
    path = f"{URL}/{uuid4()}"
    assert client.get(URL).status_code == 401
    assert client.get(path).status_code == 401
    assert client.post(URL, json=payload(identities)).status_code == 401
    assert client.put(path, json=payload(identities)).status_code == 401
    assert client.delete(path).status_code == 401


@pytest.mark.parametrize("operation", ["create", "list", "get", "update", "delete"])
def test_privileged_store_queries_are_owner_scoped(operation):
    client = MagicMock()
    query = client.table.return_value
    for method in ("select", "insert", "update", "delete", "eq", "order", "limit"):
        getattr(query, method).return_value = query
    query.execute.return_value = SimpleNamespace(data=[{"id": "plan", "athlete_id": "owner"}])
    store = SupabaseAppStore(client=client, admin_emails=set())
    fields = {"name": "Plan", "sessions": [], "incomplete": True, "athlete_id": "attacker", "kind": "active"}
    if operation == "create":
        store.create_custom_plan("owner", fields)
        query.insert.assert_called_once_with({"name": "Plan", "sessions": [], "incomplete": True, "athlete_id": "owner"})
    elif operation == "list":
        store.list_custom_plans("owner")
        query.eq.assert_called_once_with("athlete_id", "owner")
    else:
        getattr(store, f"{operation}_custom_plan")("owner", "plan", *([fields] if operation == "update" else []))
        assert [call.args for call in query.eq.call_args_list] == [("athlete_id", "owner"), ("id", "plan")]
        if operation == "update":
            query.update.assert_called_once_with({"name": "Plan", "sessions": [], "incomplete": True})
    client.table.assert_called_once_with("custom_training_plans")


def test_manual_and_legacy_round_trip(api, identities):
    client, store = api
    before = copy.deepcopy(store.plans)
    body = payload(identities)
    manual = {
        "id": str(uuid4()),
        "origin": "manual",
        "name": "My bag rounds",
        "equipment": "Heavy bag",
        "notes": "My practice",
        "dose": {
            "mode": "intervals",
            "rounds": 5,
            "work_seconds": 300,
            "rest_seconds": 30,
            "rpe": 7,
        },
    }
    body["sessions"][0]["exercises"].insert(0, manual)
    created = client.post(URL, headers=AUTH, json=body)
    assert created.status_code == 201, created.text
    plan = created.json()
    path = f"{URL}/{plan['id']}"
    assert plan["sessions"][0]["exercises"][0]["origin"] == "manual"
    assert "snapshot" not in plan["sessions"][0]["exercises"][0]
    body["sessions"][0]["exercises"].reverse()
    manual["name"] = "Renamed rounds"
    assert client.put(path, headers=AUTH, json=body).status_code == 200
    assert client.get(path, headers=OTHER).status_code == 404
    assert client.put(path, headers=OTHER, json=body).status_code == 404
    assert client.delete(path, headers=OTHER).status_code == 404
    # Persisted PR #2831 rows have neither origin nor new optional dose fields.
    row = store.custom_plans[plan["id"]]
    for session in row["sessions"]:
        for exercise in session["exercises"]:
            if exercise["origin"] == "catalogue":
                exercise.pop("origin")
                for field in (
                    "rounds",
                    "work_seconds",
                    "rpe",
                    "tempo",
                    "per_side",
                    "distance",
                    "distance_unit",
                    "prescription",
                ):
                    exercise["dose"].pop(field, None)
    assert client.get(path, headers=AUTH).status_code == 200
    assert client.get(URL, headers=AUTH).status_code == 200
    body["sessions"][0]["exercises"] = [manual]
    assert client.put(path, headers=AUTH, json=body).status_code == 200
    for field in (
        "catalogue_id",
        "snapshot",
        "verified_goals",
        "athlete_id",
        "execution_allowed",
    ):
        invalid = copy.deepcopy(body)
        invalid["sessions"][0]["exercises"][0][field] = "injected"
        assert client.put(path, headers=AUTH, json=invalid).status_code == 422
    assert store.plans == before


@pytest.mark.parametrize(
    "raw,mode,seconds",
    [
        (
            {
                "rounds": 5,
                "work_sec": 300,
                "rest_sec": 30,
                "total_minutes": 27,
                "duration": "5 x 5min rounds, 30s rest",
                "rpe": 7,
            },
            "intervals",
            300,
        ),
        ({"total_minutes": 30}, "duration", 1800),
        ({"duration": "90 seconds"}, "duration", 90),
        ({"duration": "10 min", "rpe": 6}, "duration", 600),
        ({"sets": 3, "reps": 8, "rest_sec": 90}, "reps", None),
        (
            {
                "rounds": 5,
                "work_sec": 300,
                "rest_sec": 30,
                "duration": "3x5min, 30s rest",
            },
            "text",
            None,
        ),
        ({"rounds": 5, "work_sec": 300}, "text", None),
        ({"rounds": "5", "total_minutes": 30}, "text", None),
        (
            {
                "rounds": 5,
                "work_sec": 300,
                "rest_sec": 30,
                "prescription": "3x5min, 30s rest",
            },
            "text",
            None,
        ),
        ({}, None, None),
    ],
)
def test_source_suggestions_current_explicit_evidence(
    api, identities, monkeypatch, raw, mode, seconds
):
    client, _ = api
    identity = identities["training"][0]
    record = copy.deepcopy(cache.get().by_id[identity])
    record["entry"]["raw"] = raw
    monkeypatch.setattr(cache, "get", lambda: SimpleNamespace(by_id={identity: record}))
    response = client.get(
        f"{URL}/source-suggestion", params={"catalogue_id": identity}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["source_revision"] and result["row_revision"]
    assert (result["dose"]["mode"] if result["dose"] else None) == mode
    if seconds:
        assert (
            result["dose"][
                "work_seconds" if mode == "intervals" else "duration_seconds"
            ]
            == seconds
        )
    assert (
        client.get(
            f"{URL}/source-suggestion", params={"catalogue_id": identity}
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "dose",
    [
        {"mode": "intervals", "rounds": 0},
        {"mode": "intervals", "work_seconds": 1.5},
        {"mode": "duration", "rounds": 5},
        {"mode": "text", "reps": 5},
        {"mode": "reps", "rpe": 11},
        {"mode": "duration", "distance": -1},
        {"mode": "duration", "distance_unit": "unknown"},
    ],
)
def test_new_dose_validation(api, dose):
    client, _ = api
    body = {
        "name": "Draft",
        "sessions": [
            {
                "id": str(uuid4()),
                "name": "Practice",
                "exercises": [
                    {
                        "id": str(uuid4()),
                        "origin": "manual",
                        "name": "My practice",
                        "dose": dose,
                    }
                ],
            }
        ],
    }
    assert client.post(URL, headers=AUTH, json=body).status_code == 422


def test_calendar_order_is_stable_on_save_and_legacy_reads(api):
    client, store = api
    days = ['Sunday', None, 'Wednesday', 'Monday', 'Friday', 'Monday', 'Tuesday', 'Saturday', 'Thursday', None]
    sessions = [{'id': str(uuid4()), 'name': f'Session {i}', 'weekday': day, 'exercises': [
        {'id': str(uuid4()), 'origin': 'manual', 'name': name, 'dose': {'mode': 'reps', 'sets': 2, 'reps': 5}}
        for name in ('Second drill', 'First drill')]} for i, day in enumerate(days)]
    response = client.post(URL, headers=AUTH, json={'name': 'Out of order', 'sessions': sessions})
    assert response.status_code == 201
    plan = response.json()
    expected = [3, 5, 6, 2, 8, 4, 7, 0, 1, 9]
    assert [session['id'] for session in plan['sessions']] == [sessions[i]['id'] for i in expected]
    assert [session['id'] for session in store.custom_plans[plan['id']]['sessions']] == [sessions[i]['id'] for i in expected]
    assert all([e['name'] for e in s['exercises']] == ['Second drill', 'First drill'] for s in plan['sessions'])
    # Older saved records are ordered on read without changing their identities.
    store.custom_plans[plan['id']]['sessions'] = [plan['sessions'][expected.index(i)] for i in range(len(days))]
    reopened = client.get(f"{URL}/{plan['id']}", headers=AUTH).json()
    listed = client.get(URL, headers=AUTH).json()[0]
    assert reopened['sessions'] == listed['sessions'] == plan['sessions']


@pytest.mark.parametrize("exercise_type,held", [("strength", False), ("bodyweight", False), (None, None), ("power", True)])
def test_private_custom_type_preserves_authored_dose_and_progression_clearance(exercise_type, held):
    from api.services.custom_plans import custom_open_plan_result
    from api.exercise_identity import reconcile_progression_context
    from api.structured_plan_models import StructuredTrainingPlan
    draft = {"id": str(uuid4()), "name": "Private training", "sessions": [{"id": str(uuid4()),
        "name": "Strength", "weekday": "Monday", "session_type": "strength_power", "exercises": [{
            "id": str(uuid4()), "origin": "manual", "name": "Private Bench Press", "exercise_type": exercise_type,
            "dose": {"mode": "reps", "sets": 3, "reps": 10, "load_or_intensity": "80 kg"}}]}]}
    profile = SimpleNamespace(technical_style=["boxing"], athlete_timezone="UTC", athlete_id="athlete-1")
    result = custom_open_plan_result(draft, profile, training_day="2026-11-11")
    plan = reconcile_progression_context(result["structured_plan"], result["planning_brief"])
    block = plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    assert block.get("progression_hold") is held
    assert block["sets"] == 3 and block["reps"] == 10 and block["load"]["value"] == 80
    assert not block.get("rep_range")
    assert block["athlete_dose"] == draft["sessions"][0]["exercises"][0]["dose"]
    StructuredTrainingPlan.model_validate(plan)


def test_custom_rep_ranges_are_authored_not_invented():
    from api.services.custom_plans import dose_block_fields
    assert dose_block_fields({"mode": "text", "prescription": "3x8-12", "rpe": 7})["reps"] == "8-12"
    assert dose_block_fields({"mode": "reps", "sets": 3, "reps": 10}) == {"sets": 3, "reps": 10}
