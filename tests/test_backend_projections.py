from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest
from supabase import ClientOptions, create_client

from api.compliance import evaluate_profile_compliance
from api.read_diagnostics import DiagnosticHttpClient, begin, end
from api.services.active_plan import _active_plan_overlap
from api.services.effective_structured_plan import resolve_effective_structured_plan
from api.services.today_service import check_in_possible
from api.schema_requirements import REQUIRED_FUNCTIONS, REQUIRED_PLANS_COLUMNS
from api.store import (
    ADMIN_ONLY_PLAN_COLUMNS, ATHLETE_PLAN_SELECT, PROFILE_COMPLIANCE_SELECT, TRAINING_PLAN_REMOTE_SELECT,
    TRAINING_PLAN_SELECT, SupabaseAppStore,
)
from tests.backend_read_fixtures import hybrid_fixture, HEADERS, ATHLETE, OTHER_PLAN
from tests.test_today_service import TestOpenPlanRestDayResolution as OpenPlanFixture
from tests.test_today_read_amplification import PLAN, _store_with_camp


def test_history_removes_real_data_api_plan_attempts():
    client, store, queries, transport = hybrid_fixture()
    diagnostics, token = begin()
    try:
        response = client.get("/api/training/history", headers=HEADERS)
        assert response.status_code == 200
        assert {r["title"] for r in response.json()["sessions"]} == {"Saved title"}
        assert diagnostics.snapshot()["reads"] == 2
        assert [q["table"] for q in queries] == ["session_completions", "external_training_sessions"]
    finally:
        end(token)
        client.close()
        transport.close()


@pytest.mark.parametrize("renewable", [False, True])
def test_existing_training_projection_preserves_legacy_calendar_and_title_inputs(renewable):
    store = OpenPlanFixture()._store() if renewable else _store_with_camp()
    full = deepcopy(store.plans[PLAN])
    full.update(stage2_payload={"large_generation_only": "not training"}, final_plan_text="admin only")
    compact = {key: full[key] for key in TRAINING_PLAN_SELECT.split(",") if key in full}
    assert "planning_brief" in TRAINING_PLAN_SELECT
    assert "stage2_payload" not in compact
    assert resolve_effective_structured_plan(compact) == resolve_effective_structured_plan(full)
    from api.routes.today import _session_history_title
    structured = resolve_effective_structured_plan(full)
    if structured:
        completion = {"training_day": "2026-06-04", "session_id": "2026-06-04-strength"}
        assert _session_history_title(compact, structured, completion) == _session_history_title(full, structured, completion)


def test_compact_projection_is_owned_and_full_detail_is_unchanged():
    captured = []
    def respond(request):
        captured.append(request)
        return httpx.Response(200, json=[])
    with DiagnosticHttpClient(transport=httpx.MockTransport(respond)) as client:
        store = SupabaseAppStore(create_client("https://fixture.supabase.co", "fixture", options=ClientOptions(httpx_client=client)), set())
        store.get_training_plan_for_athlete(PLAN, ATHLETE)
        store.get_plan_identity_for_athlete(PLAN, ATHLETE)
        store.get_latest_plan_status(ATHLETE)
        store.get_plan_for_athlete(PLAN, ATHLETE)
        store.get_plan(PLAN)
        store.get_latest_plan(ATHLETE)
    # Athlete reads drop the admin-only Stage-2 blobs and, for runtime reads, the
    # planner's candidate_pools; admin/worker reads stay whole.
    assert [r.url.params["select"] for r in captured] == [
        TRAINING_PLAN_REMOTE_SELECT, "id,athlete_id", "id,status,stage2_status,intake_id", ATHLETE_PLAN_SELECT, "*", "*"]
    for request in (captured[0], captured[1], captured[3]):
        assert request.url.params["id"] == f"eq.{PLAN}"
        assert request.url.params["athlete_id"] == f"eq.{ATHLETE}"
    assert captured[2].url.params["athlete_id"] == f"eq.{ATHLETE}"
    assert captured[-1].url.params["athlete_id"] == f"eq.{ATHLETE}"


@pytest.mark.parametrize("updates", [{}, {"health_data_consent": False}, {"health_consent_version": "old"},
    {"health_consent_withdrawn_at": "2099-01-01T00:00:00Z"}, {"date_of_birth": None}, {"health_consent_at": "invalid"}])
def test_compact_profile_preserves_every_compliance_verdict_and_freshness(updates):
    client, store, queries, transport = hybrid_fixture()
    try:
        store.profiles[ATHLETE].update(updates)
        full = store.get_profile(ATHLETE)
        compact = store.get_profile_compliance(ATHLETE)
        assert set(compact) == set(PROFILE_COMPLIANCE_SELECT.split(","))
        assert evaluate_profile_compliance(compact) == evaluate_profile_compliance(full)
        assert queries[-1]["full_row"] is False
        assert queries[-1]["response_bytes"] < queries[-2]["response_bytes"]
        store.profiles[ATHLETE]["health_data_consent"] = False
        assert check_in_possible(store, ATHLETE) is False
        store.get_profile_compliance = Mock(side_effect=httpx.ReadTimeout("fixture"))
        assert check_in_possible(store, ATHLETE) is True  # Existing failure behaviour.
    finally:
        client.close()
        transport.close()


def test_overlap_reuses_the_authoritative_owned_row_without_second_read():
    client, store, queries, transport = hybrid_fixture()
    try:
        assert _active_plan_overlap(store, ATHLETE, store.plans[OTHER_PLAN], current_training_day="2026-06-03")
        assert [q["table"] for q in queries] == ["profiles", "plans"]
    finally:
        client.close()
        transport.close()


def test_latest_metadata_failures_keep_existing_retry_and_full_fallback():
    selections = []
    def respond(request):
        selection = request.url.params["select"]
        selections.append(selection)
        if selection != "*":
            raise httpx.ReadTimeout("synthetic failure")
        return httpx.Response(200, json=[{"id": PLAN, "athlete_id": ATHLETE}])
    with DiagnosticHttpClient(transport=httpx.MockTransport(respond)) as client:
        store = SupabaseAppStore(create_client("https://fixture.supabase.co", "fixture", options=ClientOptions(httpx_client=client)), set())
        diagnostics, token = begin()
        try:
            assert store.get_latest_plan_status(ATHLETE)["id"] == PLAN
            assert selections == ["id,status,stage2_status,intake_id"] * 3 + ["*"]
            assert diagnostics.snapshot()["reads"] == 4
            assert diagnostics.snapshot()["failures"] == 3
        finally:
            end(token)


def test_metadata_only_route_consumers_do_not_request_full_latest_plan():
    from tests.support import _build_client
    client, store, _ = _build_client()
    store.get_latest_plan_status = Mock(return_value=None)
    store.get_latest_plan = Mock(side_effect=AssertionError("full latest plan unnecessary"))
    assert client.get("/api/today/landing", headers=HEADERS).status_code == 200
    assert client.get("/api/xp/progress", headers=HEADERS).status_code == 200
    assert store.get_latest_plan_status.call_count == 2
    assert not store.get_latest_plan.called


@pytest.mark.parametrize("stored", [None, {"weeks": []}])
def test_training_projection_keeps_real_deterministic_fallback_inputs(stored):
    brief = {"fight_date": "2026-11-12", "athlete_snapshot": {"days_until_fight": 4},
             "weekly_role_map": {"weeks": [{"week_index": 1, "phase": "TAPER",
                 "calendar_days": [{"d_day": d} for d in range(3, -1, -1)],
                 "session_roles": [{"role_key": "fight_week_freshness_day", "category": "recovery",
                     "athlete_facing_label": "Fight-week freshness", "scheduled_countdown_label": "D-3",
                     "session_index": 12}]}]}}
    full = {"id": PLAN, "athlete_id": ATHLETE, "fight_date": "2026-11-12", "planning_brief": brief,
            "plan_text": "D-3 (Monday) — Fight-week freshness\n- Easy mobility and breathing. Keep it restorative.",
            "structured_plan": stored, "stage2_payload": {"unused_generation": True}}
    compact = {key: full[key] for key in TRAINING_PLAN_SELECT.split(",") if key in full}
    expected = resolve_effective_structured_plan(full)
    assert expected is not None
    assert resolve_effective_structured_plan(compact) == expected


def test_slim_plan_selects_stay_tied_to_the_gated_schema():
    athlete = ATHLETE_PLAN_SELECT.split(",")
    # Every gated column except exactly the three admin-only blobs, so a column
    # added to the schema gate reaches athlete reads too.
    assert set(athlete) == set(REQUIRED_PLANS_COLUMNS) - set(ADMIN_ONLY_PLAN_COLUMNS)
    assert set(ADMIN_ONLY_PLAN_COLUMNS) == {"stage2_payload", "stage2_handoff_text", "stage2_retry_text"}
    assert "planning_brief" in athlete  # plan detail stamps exercise identity from candidate_pools
    # The runtime projection differs from TRAINING_PLAN_SELECT only by the alias.
    assert TRAINING_PLAN_REMOTE_SELECT.split(",") == [
        "planning_brief:planning_brief_runtime" if c == "planning_brief" else c for c in TRAINING_PLAN_SELECT.split(",")]
    assert "public.planning_brief_runtime" in REQUIRED_FUNCTIONS
