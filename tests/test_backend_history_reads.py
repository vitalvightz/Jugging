"""History evidence stays owned and frozen without fetching generation blobs."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from tests.support import _build_client
from tests.test_today_read_amplification import ATHLETE, PLAN, _add_camp

HEADERS = {"Authorization": "Bearer athlete-token"}
ENDPOINTS = ("/api/training/history", "/api/today/session-completions")


def seed(snapshot=None):
    client, store, _ = _build_client()
    _add_camp(store, athlete_id=ATHLETE, plan_id=PLAN)
    store.upsert_session_completion(ATHLETE, {
        "plan_id": PLAN, "session_id": "2026-06-04-strength", "training_day": "2026-06-04",
        "status": "done", "prescription_snapshot": snapshot,
    })
    return client, store


def titles(response, path):
    assert response.status_code == 200
    return ([r["title"] for r in response.json()["sessions"]] if path == ENDPOINTS[0]
            else [r["session_title"] for r in response.json()])


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("state", ["ready", "archived", "deleted", "inaccessible"])
def test_owned_saved_titles_need_no_current_plan_reads_or_parsing(path, state, monkeypatch):
    snapshot = {"plan_id": PLAN, "training_day": "2026-06-04",
                "session": {"session_id": "2026-06-04-strength", "title": "Frozen workout"}}
    client, store = seed(snapshot)
    if state == "deleted":
        del store.plans[PLAN]
    elif state == "inaccessible":
        store.plans[PLAN]["athlete_id"] = "another-athlete"
    else:
        store.plans[PLAN]["status"] = state
    before = deepcopy(store.session_completions)
    store.get_plan_for_athlete = Mock(side_effect=AssertionError("full read"))
    store.get_training_plan_for_athlete = Mock(side_effect=AssertionError("training read"))
    monkeypatch.setattr("api.routes.today.resolve_effective_structured_plan", Mock(side_effect=AssertionError("parse")))
    assert titles(client.get(path, headers=HEADERS), path) == ["Frozen workout"]
    assert store.session_completions == before
    assert not store.get_plan_for_athlete.called
    assert not store.get_training_plan_for_athlete.called
    # Another authenticated athlete cannot see this athlete's saved evidence.
    other = client.get(path, headers={"Authorization": "Bearer admin-token"})
    assert titles(other, path) == []


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("snapshot", [None, {}, {"session": []}, {"session": {"title": 12}},
    {"session": {"title": "  "}}, {"plan_id": "wrong", "session": {"title": "Wrong"}},
    {"training_day": "2026-06-05", "session": {"title": "Wrong"}},
    {"session": {"session_id": "wrong", "title": "Wrong"}}])
def test_invalid_or_legacy_snapshot_uses_owned_training_projection(path, snapshot):
    client, store = seed(snapshot)
    store.get_training_plan_for_athlete = Mock(wraps=store.get_training_plan_for_athlete)
    # FakeStore delegates its training projection to the full method; observe
    # the API chosen by the route without counting that test-only delegation.
    assert titles(client.get(path, headers=HEADERS), path) == ["Lower body strength"]
    store.get_training_plan_for_athlete.assert_called_once_with(PLAN, ATHLETE)


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("state", ["deleted", "inaccessible", "archived"])
def test_legacy_missing_or_foreign_plan_never_exposes_current_title(path, state):
    client, store = seed()
    if state == "deleted":
        del store.plans[PLAN]
    elif state == "inaccessible":
        store.plans[PLAN]["athlete_id"] = "another-athlete"
    else:
        store.plans[PLAN]["status"] = "archived"
    expected = "Lower body strength" if state == "archived" else ("Unlxck session" if path == ENDPOINTS[0] else None)
    assert titles(client.get(path, headers=HEADERS), path) == [expected]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_request_reuses_only_each_needed_plan_and_legacy_parse(path, monkeypatch):
    client, store = seed()
    second = "22222222-2222-2222-2222-222222222222"
    _add_camp(store, athlete_id=ATHLETE, plan_id=second)
    for plan_id, session_id, snapshot in [(PLAN, "2026-06-04-conditioning", None),
            (second, "saved-history", {"session": {"title": "Saved"}})]:
        store.upsert_session_completion(ATHLETE, {"plan_id": plan_id, "session_id": session_id,
            "training_day": "2026-06-04", "status": "done", "prescription_snapshot": snapshot})
    from api.routes.today import resolve_effective_structured_plan
    resolve = Mock(wraps=resolve_effective_structured_plan)
    monkeypatch.setattr("api.routes.today.resolve_effective_structured_plan", resolve)
    store.get_training_plan_for_athlete = Mock(wraps=store.get_training_plan_for_athlete)
    result = titles(client.get(path, headers=HEADERS), path)
    assert sorted(result) == ["Aerobic intervals", "Lower body strength", "Saved"]
    store.get_training_plan_for_athlete.assert_called_once_with(PLAN, ATHLETE)
    assert resolve.call_count == 1
    # No global/request-leaking result: next request must retry the legacy read.
    client.get(path, headers=HEADERS)
    assert store.get_training_plan_for_athlete.call_count == 2


def test_mixed_manual_prescribed_and_foreign_history_remains_scoped():
    client, store = seed({"session": {"title": "Frozen workout"}})
    for athlete, entry in [(ATHLETE, "manual-owned"), ("another-athlete", "manual-foreign")]:
        store.external_training_sessions[entry] = {"id": entry, "athlete_id": athlete,
            "training_day": "2026-06-04", "activity": "boxing", "duration_minutes": 30, "effort": "easy",
            "created_at": "2026-06-04T12:00:00Z"}
    store.get_training_plan_for_athlete = Mock(side_effect=AssertionError("unneeded plan"))
    response = client.get(ENDPOINTS[0], headers=HEADERS)
    assert sorted(titles(response, ENDPOINTS[0])) == ["Boxing", "Frozen workout"]
    assert {r["source"] for r in response.json()["sessions"]} == {"external", "unlxck"}


@pytest.mark.parametrize("path", ENDPOINTS)
def test_failed_legacy_reads_are_not_cached_between_requests(path):
    from fastapi import HTTPException
    client, store = seed()
    original = store.get_training_plan_for_athlete(PLAN, ATHLETE)
    store.get_training_plan_for_athlete = Mock(side_effect=[HTTPException(503, "Unavailable"), original])
    assert client.get(path, headers=HEADERS).status_code == 503
    assert titles(client.get(path, headers=HEADERS), path) == ["Lower body strength"]
    assert store.get_training_plan_for_athlete.call_count == 2


@pytest.mark.parametrize("snapshot", ["broken", [], 17])
def test_malformed_snapshot_container_uses_history_fallback(snapshot):
    client, store = seed()
    store.session_completions[ATHLETE][0]["prescription_snapshot"] = snapshot
    assert titles(client.get(ENDPOINTS[0], headers=HEADERS), ENDPOINTS[0]) == ["Lower body strength"]


def test_training_history_preserves_plan_session_identity_for_sparring_reports():
    client, _ = seed({"plan_id": PLAN, "training_day": "2026-06-04",
                      "session": {"session_id": "2026-06-04-strength", "title": "Hard sparring"}})
    response = client.get("/api/training/history", headers=HEADERS)
    assert response.status_code == 200
    row = response.json()["sessions"][0]
    assert row["session_id"] == "2026-06-04-strength"
    assert row["title"] == "Hard sparring"


@pytest.mark.parametrize("session_fields", [
    {"session_type": "sparring"},
    {"blocks": [{"block_type": "sparring", "display_name": "Coach-led rounds"}]},
])
def test_history_identifies_sparring_from_saved_structure_without_title_match(session_fields):
    client, _ = seed({"plan_id": PLAN, "training_day": "2026-06-04",
                      "session": {"session_id": "2026-06-04-strength", "title": "Evening practice", **session_fields}})
    response = client.get("/api/training/history", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["sessions"][0]["activity"] == "sparring"


def test_history_does_not_use_mismatched_snapshot_to_identify_sparring():
    client, _ = seed({"plan_id": "other-plan", "training_day": "2026-06-04",
                      "session": {"session_id": "2026-06-04-strength", "title": "Evening practice", "session_type": "sparring"}})
    response = client.get("/api/training/history", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["sessions"][0]["activity"] is None
