from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from api.app import create_app
from api.auth import AuthenticatedUser
from api.generation.persistence import persist_plan_and_finalize
from api.models import ManualStage2SubmissionRequest
from support import (
    FakeAuthService,
    FakeStore,
    SYSTEM_SCENARIOS,
    FakeStage2Automator,
    _build_client,
    _build_request,
    _empty_plan_planner,
    _planner,
    _review_required_result,
    _start_generation,
    finalized_result,
    stage1_result,
)
from test_structured_plan_models import _valid_plan


def _old_iso(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _override_aware_triage_resume_planner(payload: dict) -> dict:
    override = payload.get("_triage_resume_override") or {}
    # Mirror fightcamp.main: with an approved override, the planner continues
    # past the triage block and decorates why_log with both the override marker
    # and the preserved original triage decision.
    result = dict(stage1_result())
    if override.get("approved") is True:
        result["why_log"] = {
            "strength": {},
            "injury_triage_resume_override": {
                "bypassed_blocking": True,
                "triage_mode": "needs_review",
                "runtime_triage_mode": "full_plan",
            },
            "injury_triage_original": {
                "mode": "needs_review",
                "should_block_stage2": True,
            },
        }
    else:
        result["status"] = "triage_blocked"
        result["why_log"] = {"injury_triage": {"mode": "needs_review", "should_block_stage2": True}}
    return result


def test_admin_endpoints_require_admin_role():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)

    forbidden = client.get("/api/admin/athletes", headers={"Authorization": "Bearer athlete-token"})
    allowed = client.get("/api/admin/athletes", headers={"Authorization": "Bearer admin-token"})

    assert forbidden.status_code == 403
    assert allowed.status_code == 200
    assert client.get("/api/me", headers={"Authorization": "Bearer athlete-token"}).json()["effective_admin"] is False
    assert client.get("/api/me", headers={"Authorization": "Bearer admin-token"}).json()["effective_admin"] is True


def test_admin_routes_require_stored_profile_role_even_when_email_is_allowlisted():
    """Demoting an existing admin in profiles must revoke access even if their
    email still appears in the bootstrap allowlist."""
    store = FakeStore(admin_emails={"former-admin@example.com"})
    demoted_admin = AuthenticatedUser(
        user_id="demoted-admin-1",
        email="former-admin@example.com",
        full_name="Former Admin",
        metadata={},
    )
    profile = store.ensure_profile(demoted_admin)
    assert profile["role"] == "admin"
    profile["role"] = "athlete"
    store.profiles[demoted_admin.user_id] = profile

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"demoted-admin-token": demoted_admin}),
            planner=_empty_plan_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
        )
    )

    response = client.get(
        "/api/admin/athletes",
        headers={"Authorization": "Bearer demoted-admin-token"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "admin access required"

    me_response = client.get("/api/me", headers={"Authorization": "Bearer demoted-admin-token"})
    assert me_response.status_code == 200
    assert me_response.json()["profile"]["role"] == "athlete"
    assert me_response.json()["effective_admin"] is False


def test_ensure_profile_preserves_demoted_existing_role_even_when_email_is_allowlisted():
    store = FakeStore(admin_emails={"admin@example.com"})
    user = AuthenticatedUser(
        user_id="admin-demote-1",
        email="admin@example.com",
        full_name="Admin Demote",
        metadata={},
    )
    profile = store.ensure_profile(user)
    assert profile["role"] == "admin"

    profile["role"] = "athlete"
    store.profiles[user.user_id] = profile
    refreshed = store.ensure_profile(user)
    assert refreshed["role"] == "athlete"


def test_admin_routes_deny_email_in_env_allowlist_when_stored_role_is_athlete():
    """Runtime admin access requires both an allowlisted email and stored role."""
    store = FakeStore(admin_emails={"newadmin@example.com"})
    new_admin = AuthenticatedUser(
        user_id="new-admin-1",
        email="newadmin@example.com",
        full_name="New Admin",
        metadata={},
    )
    profile = store.ensure_profile(new_admin)
    # Storage still has the stale "athlete" role.
    profile["role"] = "athlete"
    store.profiles[new_admin.user_id] = profile

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"new-admin-token": new_admin}),
            planner=_empty_plan_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
        )
    )

    response = client.get(
        "/api/admin/athletes",
        headers={"Authorization": "Bearer new-admin-token"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "admin access required"


def test_admin_routes_deny_stored_admin_role_when_email_removed_from_allowlist():
    store = FakeStore(admin_emails=set())
    removed_admin = AuthenticatedUser(
        user_id="removed-admin-1",
        email="removed-admin@example.com",
        full_name="Removed Admin",
        metadata={},
    )
    profile = store.ensure_profile(removed_admin)
    profile["role"] = "admin"
    store.profiles[removed_admin.user_id] = profile

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"removed-admin-token": removed_admin}),
            planner=_empty_plan_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
        )
    )

    response = client.get(
        "/api/admin/athletes",
        headers={"Authorization": "Bearer removed-admin-token"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "admin access required"


def test_removed_allowlist_admin_cannot_use_role_bypass_on_shared_routes():
    store = FakeStore(admin_emails=set())
    athlete = AuthenticatedUser(user_id="athlete-owned-1", email="athlete@example.com", full_name="Athlete", metadata={})
    removed_admin = AuthenticatedUser(
        user_id="removed-admin-2",
        email="removed-admin@example.com",
        full_name="Removed Admin",
        metadata={},
    )
    store.ensure_profile(athlete)
    profile = store.ensure_profile(removed_admin)
    profile["role"] = "admin"
    store.profiles[removed_admin.user_id] = profile
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(),
    )
    job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id="removed-admin-cross-job",
        source="self_serve",
        request_payload=request.model_dump(mode="json"),
    )

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"removed-admin-token": removed_admin}),
            planner=_empty_plan_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
        )
    )

    plan_response = client.get(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer removed-admin-token"},
    )
    job_response = client.get(
        f"/api/generation-jobs/{job['id']}",
        headers={"Authorization": "Bearer removed-admin-token"},
    )

    assert plan_response.status_code == 404
    assert job_response.status_code == 403


def test_normal_athlete_denied_from_admin_routes():
    """Athletes with no admin allowlist entry must get 403."""
    store = FakeStore()
    athlete = AuthenticatedUser(
        user_id="athlete-only-1",
        email="athlete-only@example.com",
        full_name="Athlete Only",
        metadata={},
    )
    store.ensure_profile(athlete)

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-only-token": athlete}),
            planner=_empty_plan_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
        )
    )

    response = client.get(
        "/api/admin/athletes",
        headers={"Authorization": "Bearer athlete-only-token"},
    )
    assert response.status_code == 403


def test_admin_get_athlete_by_id_returns_profile():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-profile-1",
        email="solo@example.com",
        full_name="Solo Fighter",
        metadata={},
    )
    store.ensure_profile(athlete)

    forbidden = client.get(
        "/api/admin/athletes/athlete-profile-1",
        headers={"Authorization": "Bearer athlete-token"},
    )
    assert forbidden.status_code == 403

    not_found = client.get(
        "/api/admin/athletes/nonexistent-id",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert not_found.status_code == 404

    response = client.get(
        "/api/admin/athletes/athlete-profile-1",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["athlete_id"] == "athlete-profile-1"
    assert data["email"] == "solo@example.com"
    assert data["full_name"] == "Solo Fighter"
    assert data["plan_count"] == 0


def test_admin_can_list_and_open_flagged_plan_for_resolution():
    review_result = _review_required_result(
        final_plan_text="## PHASE 2: SPP\n- Heavy Bag Sprint Rounds - 6 x 15 sec",
        warning_code="equipment_incongruent_selection",
    )
    client, _, _ = _build_client(FakeStage2Automator(result=review_result))

    _, job = _start_generation(
        client,
        _build_request(
            {
                "equipment_access": ["bands", "bodyweight"],
                "training_availability": ["Tuesday", "Thursday", "Saturday"],
                "weekly_training_frequency": 3,
            }
        ),
    )
    plan_id = job["plan_id"]

    admin_list = client.get("/api/admin/plans", headers={"Authorization": "Bearer admin-token"})
    assert admin_list.status_code == 200
    listed_plan = next(plan for plan in admin_list.json() if plan["plan_id"] == plan_id)
    assert listed_plan["status"] == "publishable_with_flags"

    admin_detail = client.get(f"/api/plans/{plan_id}", headers={"Authorization": "Bearer admin-token"})
    assert admin_detail.status_code == 200
    assert admin_detail.json()["admin_outputs"]["stage2_retry_text"] == "repair prompt"


def test_admin_plans_support_limit_and_offset_query_params():
    client, _, _ = _build_client()

    _, first_job = _start_generation(client, _build_request({"athlete": {"full_name": "First Athlete"}}))
    _, second_job = _start_generation(client, _build_request({"athlete": {"full_name": "Second Athlete"}}))

    response = client.get(
        "/api/admin/plans?limit=1&offset=1",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["plan_id"] == first_job["plan_id"]
    assert body[0]["plan_id"] != second_job["plan_id"]


def test_admin_plans_support_server_side_search_query():
    client, _, _ = _build_client()

    _, kept_job = _start_generation(
        client, _build_request({"athlete": {"full_name": "Mariana Sokolova"}})
    )
    _start_generation(client, _build_request({"athlete": {"full_name": "Bruno Tavares"}}))

    response = client.get(
        "/api/admin/plans?q=sokolova",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [plan["plan_id"] for plan in body] == [kept_job["plan_id"]]


def test_admin_athletes_support_server_side_search_query():
    client, store, _ = _build_client()
    store.ensure_profile(
        AuthenticatedUser(
            user_id="athlete-search-1",
            email="needle@example.com",
            full_name="Needle Fighter",
            metadata={},
        )
    )
    store.ensure_profile(
        AuthenticatedUser(
            user_id="athlete-search-2",
            email="haystack@example.com",
            full_name="Haystack Fighter",
            metadata={},
        )
    )

    response = client.get(
        "/api/admin/athletes?q=needle@example.com",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [athlete["email"] for athlete in body] == ["needle@example.com"]


def test_admin_athletes_search_query_is_case_insensitive_and_partial():
    client, store, _ = _build_client()
    store.ensure_profile(
        AuthenticatedUser(
            user_id="athlete-partial-1",
            email="zoe@example.com",
            full_name="Zoe Park",
            metadata={},
        )
    )

    response = client.get(
        "/api/admin/athletes?q=ZO",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert any(athlete["email"] == "zoe@example.com" for athlete in body)


def test_admin_athletes_support_limit_and_offset_query_params():
    client, store, _ = _build_client()
    store.ensure_profile(
        AuthenticatedUser(
            user_id="athlete-extra-1",
            email="extra1@example.com",
            full_name="Extra One",
            metadata={},
        )
    )
    store.ensure_profile(
        AuthenticatedUser(
            user_id="athlete-extra-2",
            email="extra2@example.com",
            full_name="Extra Two",
            metadata={},
        )
    )

    response = client.get(
        "/api/admin/athletes?limit=1&offset=1",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1


def test_manual_stage2_submission_publishes_validated_admin_result():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="review_required",
            plan_text="",
            final_plan_text="",
            stage2_status="stage2_failed",
            stage2_retry_text="repair prompt",
            stage2_attempt_count=2,
        ),
    )

    response = client.post(
        f"/api/admin/plans/{plan['id']}/manual-stage2",
        headers={"Authorization": "Bearer admin-token"},
        json=ManualStage2SubmissionRequest(final_plan_text="# Manual GPT Final").model_dump(mode="json"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["outputs"]["plan_text"] == "# Manual GPT Final"
    assert body["admin_outputs"]["stage2_status"] == "stage2_pass"
    saved = store.get_plan(plan["id"])
    assert saved["plan_text"] == "# Manual GPT Final"
    assert saved["stage2_retry_text"] == ""


def test_manual_stage2_submission_flags_render_violations_with_repair_prompt():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="review_required",
            plan_text="",
            final_plan_text="",
            planning_brief={
                "restrictions": [
                    {
                        "restriction": "heavy_overhead_pressing",
                        "strength": "avoid",
                        "blocked_patterns": ["push press"],
                    }
                ],
                "phase_strategy": {"SPP": {"must_keep": ["rehab"]}},
                "candidate_pools": {
                    "SPP": {
                        "strength_slots": [],
                        "conditioning_slots": [],
                        "rehab_slots": [
                            {
                                "role": "rehab_ankle",
                                "selected": {"name": "Heel Raise"},
                                "alternates": [],
                            }
                        ],
                    }
                },
            },
            stage2_status="stage2_failed",
            stage2_retry_text="",
            stage2_attempt_count=2,
        ),
    )

    response = client.post(
        f"/api/admin/plans/{plan['id']}/manual-stage2",
        headers={"Authorization": "Bearer admin-token"},
        json=ManualStage2SubmissionRequest(
            final_plan_text="## PHASE 2: SPP\n- Push Press - 4x3"
        ).model_dump(mode="json"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "publishable_with_flags"
    assert body["outputs"]["plan_text"] == "## PHASE 2: SPP\n- Push Press - 4x3"
    assert body["admin_outputs"]["stage2_status"] == "stage2_pass"
    # Observational release: the finding stays visible to admins on the report
    # rather than holding the plan behind a repair prompt.
    assert body["admin_outputs"]["stage2_validator_report"]["quality_review_flags"]


def test_manual_stage2_submission_publishes_when_only_non_blocking_review_flags_exist():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="review_required",
            plan_text="",
            final_plan_text="",
            planning_brief={"athlete_model": {"sport": "boxing"}},
            stage2_status="stage2_failed",
            stage2_retry_text="",
            stage2_attempt_count=2,
        ),
    )

    response = client.post(
        f"/api/admin/plans/{plan['id']}/manual-stage2",
        headers={"Authorization": "Bearer admin-token"},
        json=ManualStage2SubmissionRequest(
            final_plan_text=(
                "## PHASE 2: SPP\n"
                "- Double-leg sprint entry - 6 x 6 sec\n"
            )
        ).model_dump(mode="json"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "publishable_with_flags"
    assert body["outputs"]["plan_text"]
    assert body["admin_outputs"]["stage2_status"] == "stage2_pass"
    assert body["admin_outputs"]["stage2_validator_report"]["review_flag_count"] >= 1


def test_manual_stage2_submission_routes_admin_review_quality_flags_without_holding():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="review_required",
            plan_text="",
            final_plan_text="",
            planning_brief={
                "athlete_model": {"sport": "boxing"},
                "phase_strategy": {"SPP": {"must_keep": ["alactic"]}},
                "candidate_pools": {
                    "SPP": {
                        "strength_slots": [],
                        "conditioning_slots": [
                            {
                                "role": "alactic",
                                "selected": {"name": "Air Bike Sprint"},
                                "alternates": [],
                            }
                        ],
                        "rehab_slots": [],
                    }
                },
            },
            stage2_status="stage2_failed",
            stage2_retry_text="",
            stage2_attempt_count=2,
        ),
    )

    response = client.post(
        f"/api/admin/plans/{plan['id']}/manual-stage2",
        headers={"Authorization": "Bearer admin-token"},
        json=ManualStage2SubmissionRequest(
            final_plan_text="## PHASE 2: SPP\n- Landmine Press - 4x5"
        ).model_dump(mode="json"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "publishable_with_flags"
    assert body["outputs"]["plan_text"] == "## PHASE 2: SPP\n- Landmine Press - 4x5"
    assert body["admin_outputs"]["stage2_status"] == "stage2_pass"
    assert body["admin_outputs"]["stage2_validator_report"]["admin_review_blocking_flags"][
        0
    ]["code"] == "missing_required_element"


def test_manual_stage2_submission_requires_admin_role():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(),
    )

    response = client.post(
        f"/api/admin/plans/{plan['id']}/manual-stage2",
        headers={"Authorization": "Bearer athlete-token"},
        json=ManualStage2SubmissionRequest(final_plan_text="# Manual GPT Final").model_dump(mode="json"),
    )

    assert response.status_code == 403


def test_admin_can_approve_review_required_plan_for_release():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="review_required",
            plan_text="",
            final_plan_text="# Held Stage 2 Output",
            stage2_status="stage2_failed",
            stage2_retry_text="repair prompt",
            stage2_attempt_count=2,
        ),
    )

    response = client.post(
        f"/api/admin/plans/{plan['id']}/approve",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["outputs"]["plan_text"] == "# Held Stage 2 Output"
    assert body["admin_outputs"]["stage2_status"] == "admin_review_approved"


def test_structured_card_rebuild_requires_admin_and_is_idempotent_while_building(
    monkeypatch,
):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    client, store, _ = _build_client()
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            structured_plan=None,
            stage2_validator_report={
                "structured_plan": {
                    "status": "invalid_fallback_used",
                    "errors": ["bad shape"],
                }
            },
        ),
    )
    background_calls: list[dict] = []

    async def _capture_background(**kwargs):
        background_calls.append(kwargs)

    monkeypatch.setattr(
        "api.routes.admin.run_structured_plan_post_processing_service",
        _capture_background,
    )

    forbidden = client.post(
        f"/api/admin/plans/{plan['id']}/structured-plan/rebuild",
        headers={"Authorization": "Bearer athlete-token"},
    )
    first = client.post(
        f"/api/admin/plans/{plan['id']}/structured-plan/rebuild",
        headers={"Authorization": "Bearer admin-token"},
    )
    second = client.post(
        f"/api/admin/plans/{plan['id']}/structured-plan/rebuild",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert forbidden.status_code == 403
    assert first.status_code == 202
    assert first.json() == {"queued": True, "plan_id": plan["id"]}
    assert second.status_code == 202
    assert second.json() == {"queued": False, "plan_id": plan["id"]}
    assert len(background_calls) == 1
    assert background_calls[0]["rebuild"] is True
    assert background_calls[0]["continue_existing_attempt"] is True
    assert store.plans[plan["id"]]["stage2_validator_report"].get(
        "structured_card_attempt_started_at"
    )


def test_structured_card_rebuild_is_noop_when_clean_card_is_live(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    client, store, _ = _build_client()
    structured_plan = _valid_plan()
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            structured_plan=structured_plan,
            schema_version=structured_plan["schema_version"],
            stage2_validator_report={"structured_plan": {"status": "valid"}},
        ),
    )

    response = client.post(
        f"/api/admin/plans/{plan['id']}/structured-plan/rebuild",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 202
    assert response.json() == {"queued": False, "plan_id": plan["id"]}


def test_structured_card_rebuild_queues_when_claimed_live_card_no_longer_decodes(
    monkeypatch,
):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    client, store, _ = _build_client()
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            structured_plan={"plan_metadata": "not-an-object"},
            schema_version="structured_training_plan.v1",
            stage2_validator_report={"structured_plan": {"status": "valid"}},
        ),
    )
    background_calls: list[dict] = []

    async def _capture_background(**kwargs):
        background_calls.append(kwargs)

    monkeypatch.setattr(
        "api.routes.admin.run_structured_plan_post_processing_service",
        _capture_background,
    )

    response = client.post(
        f"/api/admin/plans/{plan['id']}/structured-plan/rebuild",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 202
    assert response.json() == {"queued": True, "plan_id": plan["id"]}
    assert len(background_calls) == 1


def test_structured_card_rebuild_returns_not_found_for_unknown_plan(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    client, _store, _ = _build_client()

    response = client.post(
        "/api/admin/plans/missing/structured-plan/rebuild",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 404


def test_admin_reject_records_rejection_without_holding_plan():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="ready",
            plan_text="# Released Stage 2 Output",
            final_plan_text="# Released Stage 2 Output",
            stage2_status="admin_review_approved",
            stage2_retry_text="repair prompt",
            stage2_attempt_count=2,
        ),
    )

    response = client.post(
        f"/api/admin/plans/{plan['id']}/reject",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["admin_outputs"]["final_plan_text"] == "# Released Stage 2 Output"
    assert body["admin_outputs"]["stage2_status"] == "admin_review_rejected"


def test_admin_can_archive_plan_and_remove_athlete_facing_output():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="ready",
            plan_text="# Released Stage 2 Output",
            final_plan_text="# Released Stage 2 Output",
            stage2_status="admin_review_approved",
            stage2_retry_text="repair prompt",
            stage2_attempt_count=2,
        ),
    )

    response = client.post(
        f"/api/admin/plans/{plan['id']}/archive",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "archived"
    assert body["outputs"]["plan_text"] == ""
    assert body["admin_outputs"]["final_plan_text"] == "# Released Stage 2 Output"
    assert body["admin_outputs"]["stage2_status"] == "admin_archived"

    athlete_response = client.get(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )
    assert athlete_response.status_code == 404


def test_admin_archive_blocked_by_active_generation_job():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(),
    )
    job = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="active-admin-archive-guard",
        source="web_intake",
        request_payload=_build_request().model_dump(mode="json"),
        plan_id=plan["id"],
        intake_id="intake_x",
    )
    store.update_generation_job(job["id"], status="running", started_at="2026-01-01T00:00:00+00:00")

    response = client.post(
        f"/api/admin/plans/{plan['id']}/archive",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Plan has an active generation job. Cancel or wait before archiving."
    assert store.get_plan(plan["id"])["status"] != "archived"


def test_needs_review_can_be_approved_and_resumed_with_normal_generation_flow():
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    admin = AuthenticatedUser(
        user_id="admin-1",
        email="ops@unlxck.test",
        full_name="Ops Admin",
        metadata={},
    )
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_blocked",
            why_log={"injury_triage": {"mode": "needs_review", "should_block_stage2": True}},
        ),
    )
    stage2 = FakeStage2Automator(result=finalized_result())

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=stage2,
        )
    )

    resume_response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "injury details clarified"},
    )
    assert resume_response.status_code == 202
    job_id = resume_response.json()["job_id"]

    job_response = client.get(f"/api/generation-jobs/{job_id}", headers={"Authorization": "Bearer admin-token"})
    assert job_response.status_code == 200
    job = job_response.json()
    assert job["status"] == "completed"
    resume_payload = store.get_generation_job(job_id)["request_payload"]
    assert resume_payload["_triage_resume_override"]["approved"] is True
    assert resume_payload["_triage_resume_override"]["reason"] == "injury details clarified"
    assert resume_payload["_triage_resume_override"]["allowed_modes"] == ["needs_review", "restricted_rehab_only"]
    assert "_triage_resume_override" not in intake["intake"]
    assert len(stage2.calls) == 1
    assert stage2.calls[0]["plan_text"] == "# Stage 1 Draft"
    refreshed_plan = client.get(
        f"/api/plans/{blocked_plan['id']}",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert refreshed_plan.status_code == 200
    # The resumed generation updates the original blocked plan in place, so the
    # stage2_status reflects the new finalized run. The triage approval audit
    # marker (triage_regeneration_cleared) must still be preserved on why_log.
    assert refreshed_plan.json()["admin_outputs"]["stage2_status"] == "stage2_pass"
    assert refreshed_plan.json()["admin_outputs"]["why_log"]["triage_regeneration_cleared"] is True


def test_approve_and_resume_generation_blocks_duplicate_after_successful_resume():
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    admin = AuthenticatedUser(
        user_id="admin-1",
        email="ops@unlxck.test",
        full_name="Ops Admin",
        metadata={},
    )
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_blocked",
            why_log={"injury_triage": {"mode": "needs_review", "should_block_stage2": True}},
        ),
    )
    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
        )
    )

    first_response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "injury details clarified"},
    )
    assert first_response.status_code == 202

    second_response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "repeat click"},
    )
    assert second_response.status_code == 409
    assert second_response.json()["detail"] == "this blocked plan has already been approved and resumed"


def test_approve_and_resume_generation_does_not_reset_live_running_resume_job():
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_resume_approved",
            why_log={
                "injury_triage": {"mode": "needs_review", "should_block_stage2": True},
                "triage_regeneration_cleared": True,
            },
        ),
    )
    existing_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id=f"triage_resume_{blocked_plan['id']}",
        source="admin_triage_resume",
        request_payload={"existing": True, "_triage_resume_override": {"approved": True}},
        intake_id=str(intake["id"]),
        plan_id=str(blocked_plan["id"]),
    )
    store.update_generation_job(
        existing_job["id"],
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        heartbeat_at=datetime.now(timezone.utc).isoformat(),
        stage1_result={"status": "ready"},
        final_result={"status": "working"},
    )
    client = TestClient(create_app(store=store, auth_service=FakeAuthService({"admin-token": admin})))
    response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "keep polling"},
    )
    assert response.status_code == 202
    assert response.json()["job_id"] == existing_job["id"]
    refreshed = store.get_generation_job(existing_job["id"])
    assert refreshed["status"] == "running"
    assert refreshed["stage1_result"] == {"status": "ready"}
    assert refreshed["final_result"] == {"status": "working"}
    assert refreshed["request_payload"] == {"existing": True, "_triage_resume_override": {"approved": True}}


def test_approve_and_resume_generation_rejects_live_running_resume_job_with_unsafe_linkage():
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_resume_approved",
            why_log={"injury_triage": {"mode": "needs_review", "should_block_stage2": True}},
        ),
    )
    existing_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id=f"triage_resume_{blocked_plan['id']}",
        source="admin_triage_resume",
        request_payload={"existing": True},
    )
    store.update_generation_job(
        existing_job["id"],
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        heartbeat_at=datetime.now(timezone.utc).isoformat(),
        intake_id=None,
        plan_id=None,
    )
    client = TestClient(create_app(store=store, auth_service=FakeAuthService({"admin-token": admin})))
    response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "keep polling"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "existing triage resume job has unsafe linkage; create a new resume request"


def test_approve_and_resume_generation_requeues_completed_triage_blocked_resume_job():
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_resume_approved",
            why_log={"injury_triage": {"mode": "needs_review", "should_block_stage2": True}, "triage_regeneration_cleared": True},
        ),
    )
    existing = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id=f"triage_resume_{blocked_plan['id']}",
        source="admin_triage_resume",
        request_payload=request.to_payload(),
        intake_id=str(intake["id"]),
        plan_id=str(blocked_plan["id"]),
    )
    store.update_generation_job(existing["id"], status="running", started_at=datetime.now(timezone.utc).isoformat(), heartbeat_at=datetime.now(timezone.utc).isoformat())
    store.update_generation_job(existing["id"], status="completed", final_result={"status": "triage_blocked"}, completed_at=datetime.now(timezone.utc).isoformat())
    client = TestClient(create_app(store=store, auth_service=FakeAuthService({"admin-token": admin})))
    response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "retry blocked resume"},
    )
    assert response.status_code == 202
    job = store.get_generation_job(response.json()["job_id"])
    assert job["id"] == existing["id"]
    assert job["request_payload"]["_triage_resume_override"]["approved"] is True


def test_approve_and_resume_generation_creates_job_with_intake_and_plan_linked():
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    admin = AuthenticatedUser(
        user_id="admin-1",
        email="ops@unlxck.test",
        full_name="Ops Admin",
        metadata={},
    )
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_blocked",
            why_log={"injury_triage": {"mode": "needs_review", "should_block_stage2": True}},
        ),
    )
    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
        )
    )

    response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "injury details clarified"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = store.get_generation_job(job_id)
    assert job is not None
    assert job["intake_id"] == str(intake["id"])
    assert job["plan_id"] == str(blocked_plan["id"])


def _seed_triage_blocked_job(store, *, athlete_id: str, intake_id: str, request) -> dict:
    """Seed a generation_jobs row that represents a new-style triage outcome
    (no plan row, final_result carries the triage state on the job)."""
    job = store.create_or_get_generation_job(
        athlete_id=athlete_id,
        client_request_id=f"triage_seed_{intake_id}",
        source="self_serve",
        request_payload=request.model_dump(mode="json"),
        intake_id=intake_id,
    )
    store.update_generation_job(
        job["id"],
        status="running",
        started_at="2026-01-01T00:00:00+00:00",
        heartbeat_at="2026-01-01T00:00:00+00:00",
    )
    store.update_generation_job(
        job["id"],
        status="review_required",
        final_result={
            "status": "triage_blocked",
            "stage2_status": "triage_blocked",
            "why_log": {"injury_triage": {"mode": "needs_review", "should_block_stage2": True}},
        },
        heartbeat_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:00+00:00",
    )
    return store.get_generation_job(job["id"])


def test_admin_triage_queue_lists_suspended_job_without_plan_row():
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    triage_job = _seed_triage_blocked_job(
        store, athlete_id=athlete.user_id, intake_id=str(intake["id"]), request=request
    )

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=lambda payload: stage1_result(),
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    forbidden = client.get(
        "/api/admin/generation-jobs/triage",
        headers={"Authorization": "Bearer athlete-token"},
    )
    response = client.get(
        "/api/admin/generation-jobs/triage",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert forbidden.status_code == 403
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["job_id"] == triage_job["id"]
    assert body[0]["athlete_id"] == athlete.user_id
    assert body[0]["athlete_email"] == athlete.email
    assert body[0]["athlete_full_name"] == athlete.full_name
    assert body[0]["plan_id"] is None
    assert body[0]["stage2_status"] == "triage_blocked"
    assert body[0]["requires_admin_resume"] is True
    assert body[0]["request_payload_summary"]["athlete_name"] == request.athlete.full_name


def test_admin_triage_queue_hides_approved_source_job():
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    triage_job = _seed_triage_blocked_job(
        store, athlete_id=athlete.user_id, intake_id=str(intake["id"]), request=request
    )
    final_result = dict(store.get_generation_job(triage_job["id"])["final_result"])
    final_result["stage2_status"] = "triage_resume_approved"
    store.update_generation_job(triage_job["id"], final_result=final_result)

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"admin-token": admin}),
            planner=lambda payload: stage1_result(),
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.get(
        "/api/admin/generation-jobs/triage",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_admin_can_list_active_generation_jobs():
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    running_athlete = AuthenticatedUser(user_id="athlete-2", email="zoe@example.com", full_name="Zoe Park", metadata={})
    done_athlete = AuthenticatedUser(user_id="athlete-3", email="done@example.com", full_name="Done Athlete", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    store.ensure_profile(running_athlete)
    store.ensure_profile(done_athlete)
    request = _build_request()
    warning = "Profile refresh failed; plan generated from submitted intake only."
    queued_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id="active_queued",
        source="self_serve",
        request_payload=request.model_dump(mode="json"),
    )
    running_job = store.create_or_get_generation_job(
        athlete_id=running_athlete.user_id,
        client_request_id="active_running",
        source="admin_latest_intake",
        request_payload=request.model_dump(mode="json"),
    )
    now = datetime.now(timezone.utc).isoformat()
    store.update_generation_job(
        running_job["id"],
        status="running",
        started_at=now,
        heartbeat_at=now,
        progress_milestones=[
            {"code": "profile_refresh_failed_warning", "detail": warning, "meta": {"warning": True}},
        ],
    )
    terminal_job = store.create_or_get_generation_job(
        athlete_id=done_athlete.user_id,
        client_request_id="already_done",
        source="self_serve",
        request_payload=request.model_dump(mode="json"),
    )
    store.update_generation_job(terminal_job["id"], status="running", started_at=now, heartbeat_at=now)
    store.update_generation_job(terminal_job["id"], status="completed", completed_at=now)

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=lambda payload: stage1_result(),
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    forbidden = client.get(
        "/api/admin/generation-jobs/active",
        headers={"Authorization": "Bearer athlete-token"},
    )
    response = client.get(
        "/api/admin/generation-jobs/active",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert forbidden.status_code == 403
    assert response.status_code == 200
    body = response.json()
    job_ids = {job["job_id"] for job in body}
    assert queued_job["id"] in job_ids
    assert running_job["id"] in job_ids
    assert terminal_job["id"] not in job_ids
    listed_running = next(job for job in body if job["job_id"] == running_job["id"])
    assert listed_running["status"] == "running"
    assert listed_running["athlete_email"] == running_athlete.email
    assert listed_running["athlete_full_name"] == running_athlete.full_name
    assert listed_running["warnings"] == [warning]
    assert listed_running["request_payload_summary"]["athlete_name"] == request.athlete.full_name
    listed_queued = next(job for job in body if job["job_id"] == queued_job["id"])
    assert listed_queued["warnings"] == []


def test_approve_and_resume_generation_from_job_creates_resume_job_without_plan_id():
    """The new endpoint takes a generation_job_id (not a plan_id). It must
    create an admin_triage_resume job linked to the intake but NOT to any
    plan row — the resume produces a real plan only when Stage 2 succeeds."""
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    triage_job = _seed_triage_blocked_job(
        store, athlete_id=athlete.user_id, intake_id=str(intake["id"]), request=request
    )

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/generation-jobs/{triage_job['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "intake reviewed"},
    )
    assert response.status_code == 202
    body = response.json()
    resume_job = store.get_generation_job(body["job_id"])
    assert resume_job is not None
    assert resume_job["id"] != triage_job["id"]
    assert resume_job["source"] == "admin_triage_resume"
    assert resume_job["intake_id"] == str(intake["id"])
    # New endpoint must NOT pre-link a plan_id — the resume runtime creates
    # a real plan only after Stage 2 succeeds.
    assert resume_job.get("plan_id") in (None, "")
    # No plan row exists yet (no Stage 2 has run).
    assert store.plans == {}
    # Source job got the approval marker so a second approval is rejected.
    source_after = store.get_generation_job(triage_job["id"])
    final_after = source_after.get("final_result") or {}
    assert final_after.get("stage2_status") == "triage_resume_approved"
    assert isinstance(final_after.get("why_log", {}).get("triage_resume_approval"), dict)


def test_approve_and_resume_generation_from_job_rejects_non_triage_job():
    """The new endpoint refuses jobs that are not in a protected triage state."""
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    plain_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id="not-triage",
        source="self_serve",
        request_payload=request.model_dump(mode="json"),
        intake_id=str(intake["id"]),
    )
    store.update_generation_job(
        plain_job["id"],
        status="running",
        started_at="2026-01-01T00:00:00+00:00",
        heartbeat_at="2026-01-01T00:00:00+00:00",
    )
    store.update_generation_job(plain_job["id"], status="completed", final_result={"status": "ready"})

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/generation-jobs/{plain_job['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "n/a"},
    )
    assert response.status_code == 409
    assert "protected triage" in response.json().get("detail", "")


def test_approve_and_resume_generation_from_job_rejects_already_approved():
    """A second approval on the same triage job is rejected with a clear conflict."""
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    triage_job = _seed_triage_blocked_job(
        store, athlete_id=athlete.user_id, intake_id=str(intake["id"]), request=request
    )
    # Mark as already approved.
    final_result = triage_job["final_result"]
    final_result["stage2_status"] = "triage_resume_approved"
    store.update_generation_job(triage_job["id"], final_result=final_result)

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/generation-jobs/{triage_job['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "duplicate approval"},
    )
    assert response.status_code == 409
    assert "already" in response.json().get("detail", "")


def test_approve_and_resume_generation_from_job_does_not_lock_source_when_resume_persistence_fails():
    """Regression: if persisting the resume job fails, the source job MUST
    NOT carry the resume-approval marker — otherwise the source is locked
    in "already approved" with no functional resume job to drive the
    regeneration. The marker write has to happen AFTER the resume job is
    durable, not before."""
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    triage_job = _seed_triage_blocked_job(
        store, athlete_id=athlete.user_id, intake_id=str(intake["id"]), request=request
    )

    # Force resume-job creation to fail to simulate a transient DB outage.
    original_create = store.create_or_get_generation_job

    def failing_create_or_get(*args, **kwargs):
        if kwargs.get("client_request_id", "").startswith("triage_resume_job_"):
            raise RuntimeError("simulated supabase transient failure")
        return original_create(*args, **kwargs)

    store.create_or_get_generation_job = failing_create_or_get  # type: ignore[assignment]

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/generation-jobs/{triage_job['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "transient failure"},
    )
    # The request itself surfaces the failure (5xx). The contract under test
    # is the side effect on the source job, not the response code.
    assert response.status_code >= 500

    # Critical: the source job must remain "approve-able" — no resume-approval
    # marker should be written, because no resume job was durably created.
    source_after = store.get_generation_job(triage_job["id"])
    final_after = source_after.get("final_result") or {}
    why_log_after = final_after.get("why_log") or {}
    assert final_after.get("stage2_status") != "triage_resume_approved"
    assert "triage_resume_approval" not in why_log_after
    assert why_log_after.get("triage_regeneration_cleared") is not True

    # Restore the store and confirm a retry succeeds normally.
    store.create_or_get_generation_job = original_create  # type: ignore[assignment]
    retry = client.post(
        f"/api/admin/generation-jobs/{triage_job['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "retry after transient failure"},
    )
    assert retry.status_code == 202


def test_approve_and_resume_generation_from_job_rejects_resolved_resume_job_explicitly():
    """If a prior approval already produced a successful resume job under
    the deterministic client_request_id, a re-approval must surface a clear
    409 "already approved and resumed" instead of silently re-queueing the
    completed resume job via the state_machine's completed→queued path."""
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    triage_job = _seed_triage_blocked_job(
        store, athlete_id=athlete.user_id, intake_id=str(intake["id"]), request=request
    )

    # Seed a successful resume job under the deterministic client_request_id
    # WITHOUT marking the source job (simulates a half-applied earlier
    # approval that recovered after the marker write was lost).
    resume_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id=f"triage_resume_job_{triage_job['id']}",
        source="admin_triage_resume",
        request_payload=request.model_dump(mode="json"),
        intake_id=str(intake["id"]),
    )
    store.update_generation_job(
        resume_job["id"],
        status="running",
        started_at="2026-01-01T00:00:00+00:00",
        heartbeat_at="2026-01-01T00:00:00+00:00",
    )
    store.update_generation_job(
        resume_job["id"],
        status="completed",
        final_result={"status": "ready", "stage2_status": "stage2_pass"},
        completed_at="2026-01-01T00:01:00+00:00",
    )

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/generation-jobs/{triage_job['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "second attempt"},
    )
    assert response.status_code == 409
    assert "already" in response.json().get("detail", "")

    # And the successful resume_job must not have been reset.
    resume_after = store.get_generation_job(resume_job["id"])
    assert resume_after["status"] == "completed"
    assert (resume_after.get("final_result") or {}).get("status") == "ready"


def test_approve_and_resume_generation_from_job_returns_running_resume_without_wiping_progress():
    """Regression: a re-approval against an existing healthy running resume
    job must NOT reset its state. The endpoint must return the in-flight
    job as-is so its stage1_result, final_result, plan_id, and heartbeat
    state survive the additional approval call.
    """
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    triage_job = _seed_triage_blocked_job(
        store, athlete_id=athlete.user_id, intake_id=str(intake["id"]), request=request
    )

    # Seed an existing in-flight resume job under the deterministic
    # client_request_id, with partial progress that must NOT be wiped.
    existing_resume = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id=f"triage_resume_job_{triage_job['id']}",
        source="admin_triage_resume",
        request_payload=request.model_dump(mode="json"),
        intake_id=str(intake["id"]),
    )
    fresh_heartbeat = datetime.now(timezone.utc).isoformat()
    preserved_stage1 = {"status": "ready", "plan_text": "halfway done"}
    store.update_generation_job(
        existing_resume["id"],
        status="running",
        started_at=fresh_heartbeat,
        heartbeat_at=fresh_heartbeat,
        stage1_result=preserved_stage1,
    )
    pre_call_resume_count = len([
        job for job in store.generation_jobs.values()
        if str(job.get("source") or "") == "admin_triage_resume"
    ])
    assert pre_call_resume_count == 1

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/generation-jobs/{triage_job['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "second attempt while resume is still running"},
    )
    assert response.status_code == 202
    body = response.json()
    # Same in-flight job is returned, not a new one.
    assert body["job_id"] == existing_resume["id"]
    assert body["status"] == "running"

    # Progress must be intact — no reset of stage1_result, no wiped
    # heartbeat, no flip back to queued.
    after = store.get_generation_job(existing_resume["id"])
    assert after["status"] == "running"
    assert after.get("stage1_result") == preserved_stage1
    assert after.get("final_result") is None
    assert after.get("started_at") == fresh_heartbeat
    assert after.get("heartbeat_at") == fresh_heartbeat
    # Marker on the source job must NOT have been written either — the
    # in-flight resume already represents the approval; writing a fresh
    # marker would imply a fresh attempt that didn't happen.
    source_after = store.get_generation_job(triage_job["id"])
    final_after = source_after.get("final_result") or {}
    assert final_after.get("stage2_status") != "triage_resume_approved"
    why_log_after = final_after.get("why_log") or {}
    assert "triage_resume_approval" not in why_log_after

    # No duplicate resume job created.
    post_call_resume_count = len([
        job for job in store.generation_jobs.values()
        if str(job.get("source") or "") == "admin_triage_resume"
    ])
    assert post_call_resume_count == 1


def test_approve_and_resume_generation_writes_plan_triage_approval_before_scheduling():
    """The plan-based resume flow must persist triage-approval markers onto
    the plan BEFORE scheduling the runtime. The runtime reads existing
    why_log to preserve `triage_regeneration_cleared` and
    `triage_resume_approval` across Stage 2 updates; writing the markers
    after schedule risks losing the audit trail if the worker reads first.
    """
    import api.services.triage_resume_service as triage_resume_service

    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_blocked",
            why_log={"injury_triage": {"mode": "needs_review", "should_block_stage2": True}},
        ),
    )

    # Spy on relative ordering of the plan-marker write vs the scheduler call.
    call_order: list[str] = []
    original_update_triage = store.update_plan_triage_approval
    original_schedule = triage_resume_service.schedule_generation_job_if_needed

    def trace_update_triage(*args, **kwargs):
        call_order.append("update_plan_triage_approval")
        return original_update_triage(*args, **kwargs)

    async def trace_schedule(**kwargs):
        call_order.append("schedule_generation_job_if_needed")
        return await original_schedule(**kwargs)

    store.update_plan_triage_approval = trace_update_triage  # type: ignore[assignment]
    triage_resume_service.schedule_generation_job_if_needed = trace_schedule  # type: ignore[assignment]

    try:
        client = TestClient(
            create_app(
                store=store,
                auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
                planner=_planner,
                stage2_automator=FakeStage2Automator(result=finalized_result()),
                enable_in_process_generation=False,
            )
        )

        response = client.post(
            f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
            headers={"Authorization": "Bearer admin-token"},
            json={"reason": "approve for resume"},
        )
        assert response.status_code == 202
    finally:
        triage_resume_service.schedule_generation_job_if_needed = original_schedule  # type: ignore[assignment]

    triage_index = next(
        (i for i, label in enumerate(call_order) if label == "update_plan_triage_approval"),
        None,
    )
    schedule_index = next(
        (i for i, label in enumerate(call_order) if label == "schedule_generation_job_if_needed"),
        None,
    )
    assert triage_index is not None, call_order
    assert schedule_index is not None, call_order
    assert triage_index < schedule_index, call_order
    # And the plan now carries the markers.
    plan_after = store.get_plan(blocked_plan["id"])
    why_log_after = plan_after.get("why_log") or {}
    assert plan_after.get("stage2_status") == "triage_resume_approved"
    assert isinstance(why_log_after.get("triage_resume_approval"), dict)
    assert why_log_after.get("triage_regeneration_cleared") is True


def test_approve_and_resume_generation_requeues_stale_running_resume_job_without_duplicate():
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    admin = AuthenticatedUser(
        user_id="admin-1",
        email="ops@unlxck.test",
        full_name="Ops Admin",
        metadata={},
    )
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_resume_approved",
            why_log={
                "injury_triage": {"mode": "needs_review", "should_block_stage2": True},
                "triage_resume_approval": {"approved_by_email": "ops@unlxck.test"},
                "triage_regeneration_cleared": True,
            },
        ),
    )
    client_request_id = f"triage_resume_{blocked_plan['id']}"
    request_payload = request.model_dump(mode="json")
    request_payload["_triage_resume_override"] = {"approved": True}
    existing_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id=client_request_id,
        source="admin_triage_resume",
        request_payload=request_payload,
        intake_id=str(intake["id"]),
        plan_id=str(blocked_plan["id"]),
    )
    store.update_generation_job(
        existing_job["id"],
        status="running",
        attempt_count=1,
        heartbeat_at=_old_iso(),
        started_at=_old_iso(),
        stage1_result=None,
        final_result=None,
        completed_at=None,
        error="old stall",
    )
    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "retry stalled resume"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] == existing_job["id"]
    assert body["status"] == "queued"
    assert len(store.generation_jobs) == 1
    refreshed = store.get_generation_job(existing_job["id"])
    assert refreshed["status"] == "queued"
    assert refreshed["error"] is None
    assert refreshed["stage1_result"] is None
    assert refreshed["final_result"] is None
    assert refreshed["completed_at"] is None
    assert refreshed["plan_id"] == str(blocked_plan["id"])
    assert refreshed["intake_id"] == str(intake["id"])
    assert refreshed["client_request_id"] == client_request_id
    assert refreshed["heartbeat_at"]


def test_approve_and_resume_generation_requeues_failed_resume_job_without_duplicate():
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    admin = AuthenticatedUser(
        user_id="admin-1",
        email="ops@unlxck.test",
        full_name="Ops Admin",
        metadata={},
    )
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_resume_approved",
            why_log={
                "injury_triage": {"mode": "needs_review", "should_block_stage2": True},
                "triage_resume_approval": {"approved_by_email": "ops@unlxck.test"},
                "triage_regeneration_cleared": True,
            },
        ),
    )
    client_request_id = f"triage_resume_{blocked_plan['id']}"
    existing_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id=client_request_id,
        source="admin_triage_resume",
        request_payload={
            **request.model_dump(mode="json"),
            "_triage_resume_override": {"approved": True},
        },
        intake_id=str(intake["id"]),
        plan_id=str(blocked_plan["id"]),
    )
    store.update_generation_job(
        existing_job["id"],
        status="failed",
        attempt_count=1,
        heartbeat_at=_old_iso(),
        completed_at=_old_iso(),
        stage1_result=None,
        final_result=None,
        error="planner crashed before Stage 1",
    )
    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "retry failed resume"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] == existing_job["id"]
    assert body["status"] == "queued"
    assert len(store.generation_jobs) == 1
    refreshed = store.get_generation_job(existing_job["id"])
    assert refreshed["status"] == "queued"
    assert refreshed["error"] is None
    assert refreshed["stage1_result"] is None
    assert refreshed["final_result"] is None
    assert refreshed["completed_at"] is None
    assert refreshed["plan_id"] == str(blocked_plan["id"])
    assert refreshed["intake_id"] == str(intake["id"])
    assert refreshed["client_request_id"] == client_request_id


def test_approve_and_resume_generation_requeues_stage1_timeout_resume_job_without_duplicate():
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    admin = AuthenticatedUser(
        user_id="admin-1",
        email="ops@unlxck.test",
        full_name="Ops Admin",
        metadata={},
    )
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_resume_approved",
            why_log={
                "injury_triage": {"mode": "needs_review", "should_block_stage2": True},
                "triage_resume_approval": {"approved_by_email": "ops@unlxck.test"},
                "triage_regeneration_cleared": True,
            },
        ),
    )
    client_request_id = f"triage_resume_{blocked_plan['id']}"
    existing_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id=client_request_id,
        source="admin_triage_resume",
        request_payload={
            **request.model_dump(mode="json"),
            "_triage_resume_override": {"approved": True},
        },
        intake_id=str(intake["id"]),
        plan_id=str(blocked_plan["id"]),
    )
    store.update_generation_job(
        existing_job["id"],
        status="failed",
        attempt_count=1,
        heartbeat_at=_old_iso(),
        completed_at=_old_iso(),
        stage1_result=None,
        final_result=None,
        error="Stage 1 planner timed out before producing a result.",
    )
    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "retry timed out resume"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] == existing_job["id"]
    assert body["status"] == "queued"
    assert len(store.generation_jobs) == 1
    refreshed = store.get_generation_job(existing_job["id"])
    assert refreshed["status"] == "queued"
    assert refreshed["error"] is None
    assert refreshed["stage1_result"] is None
    assert refreshed["final_result"] is None
    assert refreshed["completed_at"] is None
    assert refreshed["plan_id"] == str(blocked_plan["id"])
    assert refreshed["intake_id"] == str(intake["id"])
    assert refreshed["client_request_id"] == client_request_id
    assert refreshed["request_payload"]["_triage_resume_override"]["approved"] is True
    assert refreshed["request_payload"]["_triage_resume_override"]["reason"] == "retry timed out resume"
    assert store.get_plan(blocked_plan["id"])["stage2_status"] == "triage_resume_approved"


def test_approve_and_resume_generation_returns_non_stale_running_resume_job_as_is():
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    admin = AuthenticatedUser(
        user_id="admin-1",
        email="ops@unlxck.test",
        full_name="Ops Admin",
        metadata={},
    )
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_resume_approved",
            why_log={
                "injury_triage": {"mode": "needs_review", "should_block_stage2": True},
                "triage_resume_approval": {"approved_by_email": "ops@unlxck.test"},
                "triage_regeneration_cleared": True,
            },
        ),
    )
    client_request_id = f"triage_resume_{blocked_plan['id']}"
    existing_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id=client_request_id,
        source="admin_triage_resume",
        request_payload={
            **request.model_dump(mode="json"),
            "_triage_resume_override": {"approved": True},
        },
        intake_id=str(intake["id"]),
        plan_id=str(blocked_plan["id"]),
    )
    heartbeat_at = datetime.now(timezone.utc).isoformat()
    store.update_generation_job(
        existing_job["id"],
        status="running",
        attempt_count=1,
        heartbeat_at=heartbeat_at,
        started_at=heartbeat_at,
        stage1_result=None,
        final_result=None,
        completed_at=None,
    )
    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "repeat click"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] == existing_job["id"]
    assert body["status"] == "running"
    refreshed = store.get_generation_job(existing_job["id"])
    assert refreshed["status"] == "running"
    assert refreshed["heartbeat_at"] == heartbeat_at
    assert len(store.generation_jobs) == 1


def test_approve_and_resume_generation_rejects_wrongly_linked_existing_resume_job_after_approval():
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    admin = AuthenticatedUser(
        user_id="admin-1",
        email="ops@unlxck.test",
        full_name="Ops Admin",
        metadata={},
    )
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_resume_approved",
            why_log={
                "injury_triage": {"mode": "needs_review", "should_block_stage2": True},
                "triage_resume_approval": {"approved_by_email": "ops@unlxck.test"},
                "triage_regeneration_cleared": True,
            },
        ),
    )
    existing_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id=f"triage_resume_{blocked_plan['id']}",
        source="admin_triage_resume",
        request_payload={
            **request.model_dump(mode="json"),
            "_triage_resume_override": {"approved": True},
        },
        intake_id=str(intake["id"]),
        plan_id=None,
    )
    store.update_generation_job(existing_job["id"], status="queued")
    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "repeat click"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "existing triage resume job has unsafe linkage; create a new resume request"
    assert len(store.generation_jobs) == 1
    assert store.get_generation_job(existing_job["id"])["plan_id"] is None


def test_approve_and_resume_full_flow_updates_plan_in_place_with_override_metadata():
    """End-to-end (HTTP -> worker -> plan update) lock: a `needs_review` plan
    approved via `/approve-and-resume-generation` must result in the SAME
    plan id being updated in place (no duplicate row), the resulting plan
    status must NOT be `triage_blocked`, and the why_log must carry both the
    override markers from the planner and the approval audit trail from the
    endpoint."""
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    admin = AuthenticatedUser(
        user_id="admin-1",
        email="ops@unlxck.test",
        full_name="Ops Admin",
        metadata={},
    )
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_blocked",
            plan_text="",
            final_plan_text="",
            why_log={"injury_triage": {"mode": "needs_review", "should_block_stage2": True}},
        ),
    )

    stage2 = FakeStage2Automator(
        result=finalized_result(
            why_log={
                "strength": {},
                "injury_triage_resume_override": {
                    "bypassed_blocking": True,
                    "triage_mode": "needs_review",
                    "runtime_triage_mode": "full_plan",
                },
                "injury_triage_original": {
                    "mode": "needs_review",
                    "should_block_stage2": True,
                },
            },
        )
    )
    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_override_aware_triage_resume_planner,
            stage2_automator=stage2,
        )
    )

    response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "injury details clarified"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    job = store.get_generation_job(job_id)
    assert job["status"] == "completed"
    assert job["plan_id"] == str(blocked_plan["id"])
    assert job["request_payload"]["_triage_resume_override"]["approved"] is True
    assert str(job["final_result"]["status"]) != "triage_blocked"

    plans = store.list_user_plans(athlete.user_id)
    assert len(plans) == 1, "approve-and-resume must update in place, not create a duplicate plan"
    refreshed = store.get_plan(blocked_plan["id"])
    assert refreshed["status"] != "triage_blocked"
    why_log = refreshed["why_log"]
    assert why_log["injury_triage_resume_override"]["bypassed_blocking"] is True
    assert why_log["injury_triage_original"]["mode"] == "needs_review"
    assert why_log["triage_resume_approval"]["approved_by_email"] == "ops@unlxck.test"
    assert why_log["triage_regeneration_cleared"] is True


def test_admin_triage_resume_pre_start_stale_job_preserves_plan_and_intake_links():
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    admin = AuthenticatedUser(
        user_id="admin-1",
        email="ops@unlxck.test",
        full_name="Ops Admin",
        metadata={},
    )
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_resume_approved",
            why_log={
                "injury_triage": {"mode": "needs_review", "should_block_stage2": True},
                "triage_resume_approval": {"reason": "first approval"},
            },
        ),
    )
    stale_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id=f"triage_resume_{blocked_plan['id']}",
        source="admin_triage_resume",
        request_payload=request.model_dump(mode="json"),
    )
    store.update_generation_job(
        stale_job["id"],
        status="running",
        started_at="2026-01-01T00:00:00+00:00",
        heartbeat_at="2026-01-01T00:00:00+00:00",
        progress_milestones=[],
        plan_id=blocked_plan["id"],
        intake_id=intake["id"],
    )
    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "retry stale pre-start job"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] == stale_job["id"]
    assert body["status"] == "queued"
    reset_job = store.get_generation_job(stale_job["id"])
    assert reset_job["plan_id"] == blocked_plan["id"]
    assert reset_job["intake_id"] == intake["id"]
    assert len(store.generation_jobs) == 1


def test_medical_hold_cannot_use_approve_and_resume_generation():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(
            status="triage_blocked",
            stage2_status="triage_blocked",
            why_log={"injury_triage": {"mode": "medical_hold", "should_block_stage2": True}},
        ),
    )

    response = client.post(
        f"/api/admin/plans/{blocked_plan['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "should fail"},
    )

    assert response.status_code == 409


def test_admin_state_integrity_diagnostics_reports_orphans_and_failed_resume_markers():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(user_id="athlete-diag-1", email="a1@example.com", full_name="A One", metadata={})
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    blocked_plan = store.create_plan(
        athlete_id=athlete.user_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(status="triage_blocked", stage2_status="triage_resume_approved"),
    )
    resume_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id=f"triage_resume_{blocked_plan['id']}",
        source="admin_triage_resume",
        request_payload=request.model_dump(mode="json"),
        plan_id=blocked_plan["id"],
        intake_id=str(intake["id"]),
    )
    store.update_generation_job(resume_job["id"], status="failed", error="failed resume")
    orphan_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id="orphan-terminal-diag",
        source="self_serve",
        request_payload=request.model_dump(mode="json"),
        plan_id="",
        intake_id=str(intake["id"]),
    )
    store.update_generation_job(orphan_job["id"], status="running")
    store.update_generation_job(orphan_job["id"], status="completed")

    response = client.get(
        "/api/admin/diagnostics/state-integrity?limit=25",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 25
    assert body["orphaned_terminal_job_count"] >= 1
    assert body["failed_resume_with_approved_marker_count"] >= 1


def test_curated_review_required_scenarios_are_fast_for_admin_to_resolve():
    for scenario in [item for item in SYSTEM_SCENARIOS if item.expected_resolution]:
        client, store, _ = _build_client(FakeStage2Automator(result=scenario.automator_result))
        _, job = _start_generation(client, _build_request(scenario.request_overrides))
        plan_id = job["plan_id"]

        if scenario.expected_resolution == "approve":
            resolved = client.post(
                f"/api/admin/plans/{plan_id}/approve",
                headers={"Authorization": "Bearer admin-token"},
            )
            assert resolved.status_code == 200
            assert resolved.json()["status"] == "ready"
            assert resolved.json()["admin_outputs"]["stage2_status"] == "admin_review_approved"
        elif scenario.expected_resolution == "manual_stage2":
            resolved = client.post(
                f"/api/admin/plans/{plan_id}/manual-stage2",
                headers={"Authorization": "Bearer admin-token"},
                json=ManualStage2SubmissionRequest(
                    final_plan_text=(
                        "## PHASE 3: TAPER\n"
                        "### Week 5\n"
                        "#### Neural primer\n"
                        "- Assault Bike Sprint - 4 x 6 sec\n"
                        "#### Recovery\n"
                        "- Walk + mobility\n"
                    )
                ).model_dump(mode="json"),
            )
            assert resolved.status_code == 200
            assert resolved.json()["status"] == "ready"
            assert resolved.json()["admin_outputs"]["stage2_status"] == "stage2_pass"
        else:
            raise AssertionError(f"Unexpected resolution strategy: {scenario.expected_resolution}")

        assert store.get_plan(plan_id)["status"] == "ready"

def test_admin_can_patch_latest_intake_focus_fields_and_persist():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(user_id="athlete-intake-1", email="a1@example.com", full_name="A One", metadata={})
    store.ensure_profile(athlete)
    intake = store.create_intake("athlete-intake-1", _build_request())

    response = client.patch(
        "/api/admin/athletes/athlete-intake-1/latest-intake",
        headers={"Authorization": "Bearer admin-token"},
        json={"key_goals": ["power"], "weak_areas": ["conditioning"]},
    )
    assert response.status_code == 200
    saved = store.get_intake(intake["id"])
    assert saved["intake"]["key_goals"] == ["power"]
    assert saved["intake"]["weak_areas"] == ["conditioning"]


def test_admin_patch_latest_intake_rejects_over_cap_focus():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(user_id="athlete-intake-2", email="a2@example.com", full_name="A Two", metadata={})
    store.ensure_profile(athlete)
    base = _build_request({"fight_date": (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()})
    store.create_intake("athlete-intake-2", base)

    response = client.patch(
        "/api/admin/athletes/athlete-intake-2/latest-intake",
        headers={"Authorization": "Bearer admin-token"},
        json={"key_goals": ["power", "conditioning"], "weak_areas": ["defense"]},
    )
    assert response.status_code == 409


def test_admin_patch_latest_intake_rejects_wrong_athlete_linkage():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(user_id="athlete-intake-3", email="a3@example.com", full_name="A Three", metadata={})
    store.ensure_profile(athlete)
    store.create_intake("athlete-intake-3", _build_request())
    store.intakes["athlete-intake-3"][-1]["athlete_id"] = "other-athlete"

    response = client.patch(
        "/api/admin/athletes/athlete-intake-3/latest-intake",
        headers={"Authorization": "Bearer admin-token"},
        json={"key_goals": ["power"]},
    )
    assert response.status_code == 409


def test_admin_patch_latest_intake_uses_store_update_intake():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(user_id="athlete-intake-4", email="a4@example.com", full_name="A Four", metadata={})
    store.ensure_profile(athlete)
    intake = store.create_intake("athlete-intake-4", _build_request())
    calls: list[str] = []
    original = store.update_intake

    def tracking_update_intake(intake_id: str, *, intake: dict, fight_date: str | None, technical_style: list[str]) -> dict:
        calls.append(intake_id)
        return original(intake_id, intake=intake, fight_date=fight_date, technical_style=technical_style)

    store.update_intake = tracking_update_intake  # type: ignore[assignment]

    response = client.patch(
        "/api/admin/athletes/athlete-intake-4/latest-intake",
        headers={"Authorization": "Bearer admin-token"},
        json={"key_goals": ["power"]},
    )
    assert response.status_code == 200
    assert calls == [intake["id"]]


def _seed_named_plan(store, *, name: str = "Camp Plan") -> dict:
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(),
    )
    if name:
        plan = store.rename_plan(plan["id"], name)
    return plan


def test_admin_permanent_delete_with_matching_name_hard_deletes_plan():
    client, store, _ = _build_client()
    plan = _seed_named_plan(store, name="Camp Plan")

    response = client.request(
        "DELETE",
        f"/api/admin/plans/{plan['id']}/permanent",
        headers={"Authorization": "Bearer admin-token"},
        json={"confirm_plan_name": "Camp Plan"},
    )

    assert response.status_code == 204
    assert store.get_plan(plan["id"]) is None


def test_admin_permanent_delete_with_wrong_name_returns_400_and_keeps_plan():
    client, store, _ = _build_client()
    plan = _seed_named_plan(store, name="Camp Plan")

    response = client.request(
        "DELETE",
        f"/api/admin/plans/{plan['id']}/permanent",
        headers={"Authorization": "Bearer admin-token"},
        json={"confirm_plan_name": "camp plan"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Confirmation does not match the plan name."
    assert store.get_plan(plan["id"]) is not None


def test_admin_permanent_delete_unnamed_plan_returns_400():
    client, store, _ = _build_client()
    plan = _seed_named_plan(store, name="")

    response = client.request(
        "DELETE",
        f"/api/admin/plans/{plan['id']}/permanent",
        headers={"Authorization": "Bearer admin-token"},
        json={"confirm_plan_name": "anything"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Plan has no name. Rename it before permanent deletion."
    assert store.get_plan(plan["id"]) is not None


def test_permanent_delete_requires_admin_role():
    client, store, _ = _build_client()
    plan = _seed_named_plan(store, name="Camp Plan")

    response = client.request(
        "DELETE",
        f"/api/admin/plans/{plan['id']}/permanent",
        headers={"Authorization": "Bearer athlete-token"},
        json={"confirm_plan_name": "Camp Plan"},
    )

    assert response.status_code == 403
    assert store.get_plan(plan["id"]) is not None


def test_admin_permanent_delete_blocked_by_active_generation_job():
    client, store, _ = _build_client()
    plan = _seed_named_plan(store, name="Camp Plan")
    job = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="active-permanent-delete-guard",
        source="web_intake",
        request_payload=_build_request().model_dump(mode="json"),
        plan_id=plan["id"],
        intake_id="intake_x",
    )
    store.update_generation_job(job["id"], status="running", started_at="2026-01-01T00:00:00+00:00")

    response = client.request(
        "DELETE",
        f"/api/admin/plans/{plan['id']}/permanent",
        headers={"Authorization": "Bearer admin-token"},
        json={"confirm_plan_name": "Camp Plan"},
    )

    assert response.status_code == 409
    assert store.get_plan(plan["id"]) is not None


def test_admin_can_cancel_active_generation_job_to_unblock_plan_cleanup():
    client, store, _ = _build_client()
    plan = _seed_named_plan(store, name="Camp Plan")
    job = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="active-cancel-cleanup",
        source="web_intake",
        request_payload=_build_request().model_dump(mode="json"),
        plan_id=plan["id"],
        intake_id="intake_x",
    )
    store.update_generation_job(job["id"], status="running", started_at="2026-01-01T00:00:00+00:00")
    client.app.state.active_generation_tasks.add(job["id"])

    response = client.delete(
        f"/api/admin/generation-jobs/{job['id']}",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    saved = store.get_generation_job(job["id"])
    assert saved["status"] == "failed"
    assert saved["error"] == "Generation cancelled by admin."
    assert job["id"] not in client.app.state.active_generation_tasks
    assert store.has_active_generation_job_for_plan(plan["id"]) is False


def test_admin_cancel_generation_job_requires_admin_role():
    client, store, _ = _build_client()
    job = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="active-cancel-denied",
        source="web_intake",
        request_payload=_build_request().model_dump(mode="json"),
    )
    store.update_generation_job(job["id"], status="running", started_at="2026-01-01T00:00:00+00:00")

    response = client.delete(
        f"/api/admin/generation-jobs/{job['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )

    assert response.status_code == 403
    assert store.get_generation_job(job["id"])["status"] == "running"


def test_cancelled_generation_job_does_not_persist_plan_after_stage2_finishes():
    _, store, _ = _build_client()
    request = _build_request()
    job = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="cancel-before-persist",
        source="web_intake",
        request_payload=request.model_dump(mode="json"),
    )
    running_job = store.update_generation_job(job["id"], status="running", started_at="2026-01-01T00:00:00+00:00")
    store.update_generation_job(job["id"], status="failed", error="Generation cancelled by admin.")
    milestones: list[str] = []

    async def direct_thread_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    asyncio.run(
        persist_plan_and_finalize(
            job=running_job,
            job_id=job["id"],
            athlete_id="athlete-1",
            plan_id=None,
            intake_id=None,
            job_source="web_intake",
            resume_from_job_only=False,
            admin_resume_plan_row=None,
            final_result=finalized_result(),
            request_body=request,
            store=store,
            emit_milestone=lambda code, *_args, **_kwargs: milestones.append(code),
            to_thread_with_heartbeat=direct_thread_call,
            t_start=0.0,
        )
    )

    assert store.plans == {}
    assert "generation_cancelled_before_plan_persist" in milestones


def test_admin_permanent_delete_unknown_plan_returns_404():
    client, _, _ = _build_client()

    missing = client.request(
        "DELETE",
        "/api/admin/plans/00000000-0000-4000-8000-000000000000/permanent",
        headers={"Authorization": "Bearer admin-token"},
        json={"confirm_plan_name": "Camp Plan"},
    )
    assert missing.status_code == 404

    malformed = client.request(
        "DELETE",
        "/api/admin/plans/not-a-uuid/permanent",
        headers={"Authorization": "Bearer admin-token"},
        json={"confirm_plan_name": "Camp Plan"},
    )
    assert malformed.status_code == 404


def _archive_plan(client, plan_id: str) -> None:
    response = client.post(
        f"/api/admin/plans/{plan_id}/archive",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert response.status_code == 200


def test_admin_permanent_delete_archived_plan_skips_name_confirmation():
    client, store, _ = _build_client()
    plan = _seed_named_plan(store, name="Camp Plan")
    _archive_plan(client, plan["id"])

    # No confirmation body is required once a plan is archived.
    response = client.request(
        "DELETE",
        f"/api/admin/plans/{plan['id']}/permanent",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 204
    assert store.get_plan(plan["id"]) is None


def test_admin_bulk_permanent_delete_removes_archived_and_skips_others():
    client, store, _ = _build_client()
    archived = _seed_named_plan(store, name="Archived Camp")
    live = _seed_named_plan(store, name="Live Camp")
    _archive_plan(client, archived["id"])

    response = client.post(
        "/api/admin/plans/bulk-permanent-delete",
        headers={"Authorization": "Bearer admin-token"},
        json={"plan_ids": [archived["id"], live["id"]]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == [archived["id"]]
    assert body["deleted_count"] == 1
    assert body["skipped_count"] == 1
    assert body["skipped"] == [{"plan_id": live["id"], "reason": "not_archived"}]
    assert store.get_plan(archived["id"]) is None
    assert store.get_plan(live["id"]) is not None


def test_admin_bulk_permanent_delete_requires_admin_role():
    client, store, _ = _build_client()
    archived = _seed_named_plan(store, name="Archived Camp")
    _archive_plan(client, archived["id"])

    response = client.post(
        "/api/admin/plans/bulk-permanent-delete",
        headers={"Authorization": "Bearer athlete-token"},
        json={"plan_ids": [archived["id"]]},
    )

    assert response.status_code == 403
    assert store.get_plan(archived["id"]) is not None


def test_admin_bulk_permanent_delete_empty_list_returns_422():
    client, _, _ = _build_client()

    response = client.post(
        "/api/admin/plans/bulk-permanent-delete",
        headers={"Authorization": "Bearer admin-token"},
        json={"plan_ids": []},
    )

    assert response.status_code == 422


def test_admin_bulk_permanent_delete_reports_unknown_ids_as_skipped():
    client, store, _ = _build_client()
    archived = _seed_named_plan(store, name="Archived Camp")
    _archive_plan(client, archived["id"])

    response = client.post(
        "/api/admin/plans/bulk-permanent-delete",
        headers={"Authorization": "Bearer admin-token"},
        json={"plan_ids": [archived["id"], "not-a-uuid"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == [archived["id"]]
    assert body["skipped"] == [{"plan_id": "not-a-uuid", "reason": "not_found"}]
    assert store.get_plan(archived["id"]) is None


# ---------------------------------------------------------------------------
# PR-10: keep admin review queues visible when profile service fails.
# ---------------------------------------------------------------------------


def _seed_held_plan(store, *, athlete_id: str, status: str = "held_for_review", plan_name: str = "Held Camp") -> dict:
    request = _build_request()
    intake = store.create_intake(athlete_id, request)
    return store.create_plan(
        athlete_id=athlete_id,
        intake_id=str(intake["id"]),
        request=request,
        result=finalized_result(status=status, plan_name=plan_name),
    )


def test_admin_review_queue_lists_held_plan_when_profile_lookup_succeeds():
    client, store, _ = _build_client()
    plan = _seed_held_plan(store, athlete_id="athlete-1")

    forbidden = client.get(
        "/api/admin/plans/review", headers={"Authorization": "Bearer athlete-token"}
    )
    response = client.get(
        "/api/admin/plans/review", headers={"Authorization": "Bearer admin-token"}
    )

    assert forbidden.status_code == 403
    assert response.status_code == 200
    body = response.json()
    plan_ids = {row["plan_id"] for row in body}
    assert plan["id"] in plan_ids
    held = next(row for row in body if row["plan_id"] == plan["id"])
    assert held["athlete_email"] == "ari@example.com"
    assert held["profile_unavailable"] is False


def test_admin_review_queue_does_not_build_stage2_when_prewarm_disabled(monkeypatch):
    monkeypatch.delenv("APP_ADMIN_STRUCTURED_PREWARM_ENABLED", raising=False)
    calls: list[object] = []

    def _build_default_stage2_automator():
        calls.append(object())
        raise AssertionError("admin review queue should not build Stage 2 by default")

    fake_stage2_module = types.ModuleType("api.stage2_automation")
    fake_stage2_module.build_default_stage2_automator = _build_default_stage2_automator
    monkeypatch.setitem(sys.modules, "api.stage2_automation", fake_stage2_module)

    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    store.ensure_profile(admin)
    plan = _seed_held_plan(store, athlete_id=athlete.user_id)
    app = create_app(
        store=store,
        auth_service=FakeAuthService({"admin-token": admin}),
        planner=_planner,
        stage2_automator=None,
        enable_in_process_generation=False,
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/admin/plans/review", headers={"Authorization": "Bearer admin-token"}
        )

    assert response.status_code == 200
    assert any(row["plan_id"] == plan["id"] for row in response.json())
    assert calls == []
    assert app.state.stage2_automator is None


def test_admin_review_queue_renders_when_profile_lookup_fails():
    client, store, _ = _build_client()
    plan = _seed_held_plan(store, athlete_id="athlete-1")
    store.fail_profile_enrichment = True

    response = client.get(
        "/api/admin/plans/review", headers={"Authorization": "Bearer admin-token"}
    )

    assert response.status_code == 200
    body = response.json()
    held = next(row for row in body if row["plan_id"] == plan["id"])
    # The held plan still renders and stays actionable, with the athlete id as a
    # fallback identifier and a single profile-unavailable marker.
    assert held["athlete_id"] == "athlete-1"
    assert held["athlete_email"] == ""
    assert held["profile_unavailable"] is True


def test_admin_triage_queue_renders_when_profile_lookup_fails():
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    triage_job = _seed_triage_blocked_job(
        store, athlete_id=athlete.user_id, intake_id=str(intake["id"]), request=request
    )
    store.fail_profile_enrichment = True

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"admin-token": admin}),
            planner=lambda payload: stage1_result(),
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.get(
        "/api/admin/generation-jobs/triage", headers={"Authorization": "Bearer admin-token"}
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    job = body[0]
    # Resume stays available because the job itself is valid; only the athlete
    # detail is degraded to the id fallback.
    assert job["job_id"] == triage_job["id"]
    assert job["athlete_id"] == athlete.user_id
    assert job["athlete_email"] == ""
    assert job["requires_admin_resume"] is True
    assert job["profile_unavailable"] is True


def test_admin_active_queue_renders_when_profile_lookup_fails():
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    queued_job = store.create_or_get_generation_job(
        athlete_id=athlete.user_id,
        client_request_id="active_degraded",
        source="self_serve",
        request_payload=request.model_dump(mode="json"),
    )
    store.fail_profile_enrichment = True

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"admin-token": admin}),
            planner=lambda payload: stage1_result(),
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.get(
        "/api/admin/generation-jobs/active", headers={"Authorization": "Bearer admin-token"}
    )

    assert response.status_code == 200
    body = response.json()
    job = next(row for row in body if row["job_id"] == queued_job["id"])
    assert job["athlete_id"] == athlete.user_id
    assert job["athlete_email"] == ""
    assert job["profile_unavailable"] is True


def test_admin_review_queue_requires_admin_role():
    client, store, _ = _build_client()
    _seed_held_plan(store, athlete_id="athlete-1")

    response = client.get(
        "/api/admin/plans/review", headers={"Authorization": "Bearer athlete-token"}
    )

    assert response.status_code == 403


def _seed_restricted_rehab_triage_job(store, *, athlete_id: str, intake_id: str, request) -> dict:
    """A `restricted_rehab_only` triage outcome that lives only on the job.

    Mirrors production job 7de498af-6ba8-4640-8651-30ca1105edc4: multiple
    parsed injuries, no plan row, Stage 2 skipped.
    """
    job = store.create_or_get_generation_job(
        athlete_id=athlete_id,
        client_request_id=f"triage_rehab_seed_{intake_id}",
        source="self_serve",
        request_payload=request.model_dump(mode="json"),
        intake_id=intake_id,
    )
    store.update_generation_job(
        job["id"],
        status="running",
        started_at="2026-01-01T00:00:00+00:00",
        heartbeat_at="2026-01-01T00:00:00+00:00",
    )
    store.update_generation_job(
        job["id"],
        status="review_required",
        final_result={
            "status": "triage_blocked",
            "stage2_status": "triage_blocked",
            "why_log": {
                "injury_triage": {
                    "mode": "restricted_rehab_only",
                    "should_block_stage2": True,
                    "parsed_injury_count": 4,
                }
            },
        },
        heartbeat_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:00+00:00",
    )
    return store.get_generation_job(job["id"])


def test_restricted_rehab_only_multi_injury_job_can_be_approved_and_resumed():
    """restricted_rehab_only + multiple injuries is a resumable triage state.

    This is the production shape that timed out in Stage 1: four parsed
    injuries, a job-only triage outcome, and an admin approval that has to
    produce a runnable resume job carrying the approval override.
    """
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request(
        {
            "injuries": (
                "left shoulder impingement pressing overhead; "
                "right knee acl reconstruction, still unstable; "
                "lower back disc herniation with nerve pain; "
                "rolled right ankle, still swollen"
            )
        }
    )
    intake = store.create_intake(athlete.user_id, request)
    triage_job = _seed_restricted_rehab_triage_job(
        store, athlete_id=athlete.user_id, intake_id=str(intake["id"]), request=request
    )

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )

    response = client.post(
        f"/api/admin/generation-jobs/{triage_job['id']}/approve-and-resume-generation",
        headers={"Authorization": "Bearer admin-token"},
        json={"reason": "rehab plan reviewed"},
    )

    assert response.status_code == 202
    resume_job = store.get_generation_job(response.json()["job_id"])
    assert resume_job["source"] == "admin_triage_resume"
    assert resume_job["status"] == "queued"
    assert resume_job["intake_id"] == str(intake["id"])
    # Job-only triage: the resume job carries no plan linkage, because no
    # plan row exists yet. The runtime stamps plan_id once Stage 2 saves one
    # (see test_admin_triage_resume_without_plan_id_creates_real_plan_only_after_stage2_pass).
    assert resume_job.get("plan_id") in (None, "")
    override = (resume_job.get("request_payload") or {}).get("_triage_resume_override") or {}
    assert override.get("approved") is True
    assert "restricted_rehab_only" in (override.get("allowed_modes") or [])


def test_restricted_rehab_only_job_cannot_be_approved_twice():
    """The approval marker must gate re-approval for this mode too."""
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    admin = AuthenticatedUser(user_id="admin-1", email="ops@unlxck.test", full_name="Ops Admin", metadata={})
    store = FakeStore()
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    triage_job = _seed_restricted_rehab_triage_job(
        store, athlete_id=athlete.user_id, intake_id=str(intake["id"]), request=request
    )

    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=FakeStage2Automator(result=finalized_result()),
            enable_in_process_generation=False,
        )
    )
    headers = {"Authorization": "Bearer admin-token"}
    first = client.post(
        f"/api/admin/generation-jobs/{triage_job['id']}/approve-and-resume-generation",
        headers=headers,
        json={"reason": "rehab plan reviewed"},
    )
    second = client.post(
        f"/api/admin/generation-jobs/{triage_job['id']}/approve-and-resume-generation",
        headers=headers,
        json={"reason": "again"},
    )

    assert first.status_code == 202
    assert second.status_code == 409


def test_admin_rejects_suspended_build_and_cannot_resume_it():
    client, store, _ = _build_client(enable_in_process_generation=False)
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari", metadata={})
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    job = _seed_triage_blocked_job(store, athlete_id=athlete.user_id, intake_id=str(intake["id"]), request=request)
    endpoint = f"/api/admin/generation-jobs/{job['id']}/reject"
    assert client.post(endpoint, headers={"Authorization": "Bearer athlete-token"}).status_code == 403
    response = client.post(endpoint, headers={"Authorization": "Bearer admin-token"})
    assert response.status_code == 200
    saved = store.get_generation_job(job["id"])
    assert saved["status"] == "failed"
    assert saved["final_result"]["stage2_status"] == "admin_review_rejected"
    assert saved["final_result"]["why_log"]["injury_triage"] == job["final_result"]["why_log"]["injury_triage"]
    assert saved["final_result"]["why_log"]["admin_rejection"]["rejected_by"]
    queue = client.get("/api/admin/generation-jobs/triage", headers={"Authorization": "Bearer admin-token"})
    assert all(item["job_id"] != job["id"] for item in queue.json())
    assert client.post(endpoint, headers={"Authorization": "Bearer admin-token"}).status_code == 409
    resume = client.post(f"/api/admin/generation-jobs/{job['id']}/approve-and-resume-generation",
                         headers={"Authorization": "Bearer admin-token"}, json={"reason": "reviewed"})
    assert resume.status_code == 409


def test_admin_cannot_reject_running_or_approved_build():
    client, store, _ = _build_client(enable_in_process_generation=False)
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari", metadata={})
    store.ensure_profile(athlete)
    request = _build_request()
    intake = store.create_intake(athlete.user_id, request)
    job = _seed_triage_blocked_job(store, athlete_id=athlete.user_id, intake_id=str(intake["id"]), request=request)
    endpoint = f"/api/admin/generation-jobs/{job['id']}/reject"
    headers = {"Authorization": "Bearer admin-token"}
    running = store.create_or_get_generation_job(athlete_id=athlete.user_id,
        client_request_id="reject_running", source="self_serve", request_payload=request.model_dump(mode="json"))
    store.update_generation_job(running["id"], status="running")
    assert client.post(f"/api/admin/generation-jobs/{running['id']}/reject", headers=headers).status_code == 409
    final_result = dict(job["final_result"])
    final_result["why_log"] = {**final_result["why_log"], "triage_resume_approval": {"approved": True}}
    store.update_generation_job(job["id"], status="review_required", final_result=final_result)
    assert client.post(endpoint, headers=headers).status_code == 409
    assert client.post("/api/admin/generation-jobs/job_missing/reject", headers=headers).status_code == 404
