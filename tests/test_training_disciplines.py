"""Optional sport and session type on manual training: detail, never workload or clearance."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from api.contracts.external_training import ExternalTrainingCreate
from api.contracts.training_disciplines import SPORTS, discipline_error, training_title
from api.services.sparring_readiness_snapshot import annotate_payload_with_sparring_readiness
from api.services.training_history import create_external_training
from fightcamp.completed_workload import completed_workload
from tests.support import _build_client, FakeStore
from tests.test_api_sparring_log import _post

AUTH = {"Authorization": "Bearer athlete-token"}
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
DAY = "2026-10-10"


@pytest.fixture
def api():
    client, store, _ = _build_client()
    return client, store


def body(**changes):
    return {"client_request_id": str(uuid4()), "activity": "boxing", "duration_minutes": 60, "effort": "moderate", **changes}


def log(client, **changes):
    response = client.post("/api/training/sessions", headers=AUTH, json=body(**changes))
    assert response.status_code == 201, response.text
    return response.json()


def history(client):
    response = client.get("/api/training/history", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["sessions"]


def test_basic_log_needs_no_sport_and_stores_none(api):
    client, store = api
    row = log(client, activity="sparring", effort="hard")
    assert (row["title"], row["sport"], row["session_type"]) == ("Sparring", None, None)
    # Nothing is guessed into the stored record.
    stored = store.external_training_sessions[row["id"]]
    assert "sport" not in stored and "session_type" not in stored


@pytest.mark.parametrize("fields,title", [
    ({"sport": "boxing", "session_type": "pads", "activity": "boxing", "duration_minutes": 60}, "Boxing · Pads"),
    ({"sport": "bjj", "session_type": "rolling", "activity": "other", "duration_minutes": 90}, "BJJ · Rolling"),
    ({"sport": "muay_thai", "session_type": "clinch", "activity": "other"}, "Muay Thai · Clinch"),
    ({"sport": "kickboxing", "session_type": "sparring", "activity": "sparring"}, "Kickboxing · Sparring"),
    ({"sport": "mma", "session_type": "integrated", "activity": "other"}, "MMA · Integrated MMA"),
    ({"sport": "wrestling", "session_type": "live", "activity": "other"}, "Wrestling · Live wrestling"),
    ({"sport": "muay_thai", "activity": "other"}, "Muay Thai"),
])
def test_each_discipline_is_stored_and_shown_as_logged(api, fields, title):
    client, _ = api
    row = log(client, **fields)
    assert row["title"] == title
    assert {key: row[key] for key in fields} == fields
    [listed] = history(client)
    assert listed["title"] == title and listed["sport"] == fields["sport"]


def test_bjj_rolling_is_never_boxing_or_head_contact_sparring(api):
    client, _ = api
    for activity in ("boxing", "sparring"):
        response = client.post("/api/training/sessions", headers=AUTH,
                               json=body(sport="bjj", session_type="rolling", activity=activity, duration_minutes=90))
        assert response.status_code == 422
    row = log(client, sport="bjj", session_type="rolling", activity="other", duration_minutes=90)
    assert row["activity"] == "other" and "Boxing" not in row["title"]


@pytest.mark.parametrize("change", [
    {"sport": "boxing", "session_type": "clinch"},  # not a boxing session type
    {"session_type": "pads"},  # session type without a sport
    {"sport": "judo"},
    {"sport": "boxing", "session_type": "sparring", "activity": "boxing"},
    {"sport": "muay_thai", "activity": "boxing"},
    {"sport": "mma", "activity": "sparring"},  # sparring is only the explicit session type
    {"sport": "boxing", "activity": "strength"},  # S&C stays the existing activities, without a sport
    {"sport": "Boxing"},
])
def test_inconsistent_classification_is_rejected(api, change):
    client, store = api
    response = client.post("/api/training/sessions", headers=AUTH, json=body(**change))
    assert response.status_code == 422
    assert not store.external_training_sessions


def test_mma_striking_and_wrestling_on_one_day_stay_two_sessions(api):
    client, _ = api
    striking = log(client, sport="mma", session_type="striking", activity="other", duration_minutes=60)
    wrestling = log(client, sport="mma", session_type="wrestling", activity="other", duration_minutes=45)
    rows = history(client)
    assert {row["id"] for row in rows} == {striking["id"], wrestling["id"]}
    assert {row["title"] for row in rows} == {"MMA · Striking", "MMA · Wrestling"}
    assert {row["training_day"] for row in rows} == {striking["training_day"]}


def test_two_disciplines_on_the_same_backdated_day(api):
    client, _ = api
    boxing = log(client, sport="boxing", session_type="pads", training_day="2026-10-01")
    bjj = log(client, sport="bjj", session_type="drilling", activity="other", training_day="2026-10-01")
    assert boxing["training_day"] == bjj["training_day"] == "2026-10-01"
    assert [row["sport"] for row in history(client)] == ["bjj", "boxing"]


def test_retry_with_sport_is_idempotent_and_a_changed_sport_conflicts(api):
    client, store = api
    payload = body(sport="muay_thai", session_type="pads", activity="other")
    first = client.post("/api/training/sessions", headers=AUTH, json=payload)
    retry = client.post("/api/training/sessions", headers=AUTH, json=payload)
    assert first.status_code == retry.status_code == 201 and retry.json()["id"] == first.json()["id"]
    changed = client.post("/api/training/sessions", headers=AUTH, json={**payload, "session_type": "clinch"})
    assert changed.status_code == 409
    assert len(store.external_training_sessions) == 1


def test_edit_changes_or_clears_the_classification_and_keeps_identity(api):
    client, store = api
    row = log(client, sport="boxing", session_type="pads", training_day="2026-10-02")
    store.external_training_sessions[row["id"]]["takeaway"] = "Keep hands up"
    edited = client.patch(f"/api/training/sessions/{row['id']}", headers=AUTH, json={
        "activity": "other", "sport": "muay_thai", "session_type": "clinch", "duration_minutes": 45, "effort": "hard"})
    assert edited.status_code == 200, edited.text
    assert (edited.json()["id"], edited.json()["training_day"]) == (row["id"], "2026-10-02")
    assert (edited.json()["title"], edited.json()["takeaway"]) == ("Muay Thai · Clinch", "Keep hands up")
    rejected = client.patch(f"/api/training/sessions/{row['id']}", headers=AUTH, json={
        "activity": "sparring", "sport": "bjj", "session_type": "rolling", "duration_minutes": 45, "effort": "hard"})
    assert rejected.status_code == 422
    cleared = client.patch(f"/api/training/sessions/{row['id']}", headers=AUTH, json={
        "activity": "roadwork", "duration_minutes": 30, "effort": "easy"})
    assert cleared.status_code == 200
    assert (cleared.json()["title"], cleared.json()["sport"], cleared.json()["session_type"]) == ("Roadwork", None, None)
    assert client.delete(f"/api/training/sessions/{row['id']}", headers=AUTH).status_code == 204
    assert not history(client)


def test_legacy_rows_without_the_new_columns_load_edit_and_keep_their_label(api):
    client, store = api
    # Rows written before the migration: no sport or session_type keys at all.
    for activity in ("boxing", "sparring", "strength", "other"):
        entry = str(uuid4())
        store.external_training_sessions[entry] = {
            "id": entry, "athlete_id": "athlete-1", "client_request_id": str(uuid4()), "request_fingerprint": "a" * 64,
            "training_day": "2026-10-05", "activity": activity, "duration_minutes": 60, "effort": "hard",
            "created_at": f"2026-10-05T0{len(store.external_training_sessions)}:00:00+00:00", "deleted_at": None}
    rows = history(client)
    assert sorted(row["title"] for row in rows) == ["Boxing", "Other", "Sparring", "Strength"]
    assert all(row["sport"] is None and row["session_type"] is None for row in rows)
    boxing = next(row for row in rows if row["activity"] == "boxing")
    edited = client.patch(f"/api/training/sessions/{boxing['id']}", headers=AUTH,
                          json={"activity": "boxing", "duration_minutes": 45, "effort": "hard"})
    assert edited.status_code == 200 and edited.json()["title"] == "Boxing" and edited.json()["sport"] is None


def test_only_striking_sparring_links_the_sparring_follow_up(api):
    client, store = api
    boxing_sparring = log(client, sport="boxing", session_type="sparring", activity="sparring", effort="hard")
    rolling = log(client, sport="bjj", session_type="rolling", activity="other", effort="hard")
    detail = {"plan_id": None, "source": "free", "planned_intensity": None, "round_seconds": None}
    linked = _post(client, **detail, external_session_id=boxing_sparring["id"])
    assert linked.status_code == 201, linked.text
    # Head contact is still asked, never inferred from the session type.
    assert linked.json()["log"]["external_session_id"] == boxing_sparring["id"]
    assert _post(client, **detail, external_session_id=rolling["id"]).status_code == 404
    assert len(store.sparring_logs) == 1


def test_workload_is_unchanged_by_classification():
    plain, classified = FakeStore(), FakeStore()
    for store, extra in ((plain, {"activity": "other"}),
                         (classified, {"activity": "other", "sport": "bjj", "session_type": "rolling"})):
        for day in ("2026-10-08", DAY):
            create_external_training(store, "athlete-1", "Europe/London", ExternalTrainingCreate(**body(
                **extra, effort="hard", duration_minutes=90, training_day=day)), now=NOW)
    rows = {name: [dict(row, source="external", status="done") for row in store.external_training_sessions.values()]
            for name, store in (("plain", plain), ("classified", classified))}
    assert completed_workload(rows["plain"], DAY) == completed_workload(rows["classified"], DAY) == {
        "sessions": 2, "demanding_sessions": 2, "actual_minutes": 180, "hard_minutes": 180}


def test_generation_history_reads_legacy_and_classified_rows_as_complete():
    store = FakeStore()
    create_external_training(store, "athlete-1", "Europe/London", ExternalTrainingCreate(**body(
        activity="sparring", effort="hard")), now=NOW)
    create_external_training(store, "athlete-1", "Europe/London", ExternalTrainingCreate(**body(
        sport="mma", session_type="sparring", activity="sparring", effort="hard")), now=NOW)
    payload = annotate_payload_with_sparring_readiness({"athlete": {}}, store=store, athlete_id="athlete-1", training_day=DAY)
    evidence = payload["_sparring_readiness"]["training_history"]
    assert evidence["available"] is True
    assert evidence["workload"] == {"sessions": 2, "demanding_sessions": 2, "actual_minutes": 120, "hard_minutes": 120}
    # The labels stay out of the readiness evidence.
    assert all("sport" not in row and "session_type" not in row for row in evidence["sessions"])


def test_titles_and_errors_come_from_the_shared_taxonomy():
    assert training_title({"activity": "boxing"}) == "Boxing"
    assert training_title({"activity": "other", "sport": "bjj", "session_type": None}) == "BJJ"
    assert discipline_error("other", None, None) is None
    assert discipline_error("boxing", "boxing", None) is None
    assert discipline_error("other", "boxing", None)


def test_database_constraint_matches_the_shared_taxonomy():
    shared = json.loads((ROOT / "shared/training-disciplines.json").read_text())["sports"]
    expected = {(sport["id"], "", sport["activity"]) for sport in shared} | {
        (sport["id"], kind["id"], kind.get("activity", sport["activity"])) for sport in shared for kind in sport["session_types"]}
    assert {(sport, "", entry["activity"]) for sport, entry in SPORTS.items()} <= expected
    for path in ("supabase/migrations/20261019121808_add_external_training_discipline.sql", "supabase/schema.sql"):
        sql = (ROOT / path).read_text()
        start = sql.index("constraint external_training_sessions_discipline check")
        block = sql[start:sql.index(")))", start) + 3]
        assert set(re.findall(r"\('([a-z_]+)', '([a-z_]*)', '([a-z]+)'\)", block)) == expected, path
