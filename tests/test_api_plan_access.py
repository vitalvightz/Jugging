from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from api.auth import AuthenticatedUser
from api.models import PlanRenameRequest
from api.routes.generation_jobs import _validate_generation_job_id
from api.routes import plans as plan_routes
from support import advisory_planning_brief, _build_client, _build_request, finalized_result, stage1_result


def _weekly_schedule_planning_brief() -> dict:
    return {
        "schema_version": "planning_brief.v1",
        "weekly_role_map": {
            "weeks": [
                {
                    "phase": "SPP",
                    "declared_hard_sparring_days": ["Monday", "Wednesday"],
                    "declared_support_work_days": ["Tuesday"],
                    "hard_sparring_plan": [
                        {
                            "day": "Monday",
                            "hard_day_class": "primary_hard",
                            "effective_load": "hard",
                            "status": "hard_as_planned",
                            "reason": "",
                            "reason_codes": [],
                        },
                        {
                            "day": "Wednesday",
                            "hard_day_class": "managed_hard",
                            "effective_load": "reduced",
                            "status": "deload_suggested",
                            "reason": "high fatigue",
                            "reason_codes": ["high_fatigue"],
                            "coach_note": "Keep the rounds controlled.",
                        },
                    ],
                }
            ]
        },
    }


def _structured_date_range(start: str, end: str) -> dict:
    return {
        "schema_version": "structured_plan.v1",
        "weeks": [
            {
                "week_id": "wk-1",
                "week_index": 1,
                "phase_label": "SPP",
                "start_date": start,
                "end_date": end,
                "days": [{"date": start}, {"date": end}],
            }
        ],
    }


def test_athlete_cannot_read_another_athlete_plan():
    client, store, _ = _build_client()
    other_user = AuthenticatedUser(
        user_id="athlete-2",
        email="other@example.com",
        full_name="Other Athlete",
        metadata={},
    )
    store.ensure_profile(other_user)
    plan = store.create_plan(
        athlete_id="athlete-2",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(),
    )

    response = client.get(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )

    assert response.status_code == 404


def test_athlete_can_read_weekly_schedule_for_their_plan_and_latest_plan():
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
        result=finalized_result(planning_brief=_weekly_schedule_planning_brief()),
    )
    store.set_active_plan_id("athlete-1", plan["id"])

    response = client.get(
        f"/api/plans/{plan['id']}/weekly-schedule",
        headers={"Authorization": "Bearer athlete-token"},
    )
    latest_response = client.get(
        "/api/plans/latest/weekly-schedule",
        headers={"Authorization": "Bearer athlete-token"},
    )

    assert response.status_code == 200
    assert latest_response.status_code == 200
    body = response.json()
    assert body["plan_id"] == plan["id"]
    assert body["week_index"] == 0
    assert body["week_count"] == 1
    assert body["phase"] == "SPP"
    assert [day["weekday"] for day in body["days"]] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert body["days"][0]["sparring_day_class"] == "primary_hard"
    assert body["days"][1]["sparring_day_class"] == "none"
    assert body["days"][2]["sparring_day_class"] == "managed_hard"
    assert body["days"][2]["coach_note"] == "Keep the rounds controlled."
    assert latest_response.json() == body


def test_athlete_cannot_read_another_athlete_weekly_schedule():
    client, store, _ = _build_client()
    owner = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    other_user = AuthenticatedUser(
        user_id="athlete-2",
        email="other@example.com",
        full_name="Other Athlete",
        metadata={},
    )
    store.ensure_profile(owner)
    store.ensure_profile(other_user)
    client.app.state.auth_service.users_by_token["other-token"] = other_user
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(planning_brief=_weekly_schedule_planning_brief()),
    )

    response = client.get(
        f"/api/plans/{plan['id']}/weekly-schedule",
        headers={"Authorization": "Bearer other-token"},
    )

    assert response.status_code == 404


def test_weekly_schedule_returns_404_for_plan_without_weekly_role_map():
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
        result=finalized_result(planning_brief={"schema_version": "planning_brief.v1"}),
    )

    response = client.get(
        f"/api/plans/{plan['id']}/weekly-schedule",
        headers={"Authorization": "Bearer athlete-token"},
    )

    assert response.status_code == 404


def test_archived_plan_weekly_schedule_is_hidden_from_athlete():
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
            status="archived",
            stage2_status="admin_archived",
            planning_brief=_weekly_schedule_planning_brief(),
        ),
    )

    response = client.get(
        f"/api/plans/{plan['id']}/weekly-schedule",
        headers={"Authorization": "Bearer athlete-token"},
    )

    assert response.status_code == 404


def test_admin_can_view_internal_plan_outputs():
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
            stage2_retry_text="repair prompt",
            planning_brief={"schema_version": "planning_brief.v1"},
            parsing_metadata={"athlete_timezone": {"source": "defaulted_missing"}},
        ),
    )

    response = client.get(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    admin_outputs = response.json()["admin_outputs"]
    assert admin_outputs["stage2_payload"] == {"ok": True}
    assert admin_outputs["draft_plan_text"] == "# Stage 1 Draft"
    assert admin_outputs["stage2_retry_text"] == "repair prompt"
    assert admin_outputs["stage2_status"] == "stage2_pass"
    assert admin_outputs["parsing_metadata"] == {"athlete_timezone": {"source": "defaulted_missing"}}


def test_create_plan_held_for_review_keeps_athlete_plan_text_empty():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="held_for_review",
            plan_text="",
            final_plan_text="# Internal held plan",
            stage2_status="stage2_failed",
        ),
    )
    assert plan["plan_text"] == ""
    assert plan["final_plan_text"] == "# Internal held plan"


def test_update_plan_stage2_publishable_with_flags_populates_athlete_plan_text():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    store.ensure_profile(athlete)
    plan = store.create_plan(athlete_id="athlete-1", intake_id="intake_x", request=_build_request(), result=finalized_result())
    updated = store.update_plan_stage2(
        plan["id"],
        finalized_result(
            status="publishable_with_flags",
            plan_text="# Publishable with minor flags",
            final_plan_text="# Publishable with minor flags",
        ),
    )
    assert updated["plan_text"] == "# Publishable with minor flags"


def test_legacy_review_required_publishable_row_uses_final_plan_text_for_athlete_visibility():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="review_required",
            plan_text="",
            final_plan_text="# Legacy final plan",
            stage2_validator_report={"errors": [], "warnings": [{"code": "generic_filler_phrase"}]},
        ),
    )
    response = client.get(f"/api/plans/{plan['id']}", headers={"Authorization": "Bearer athlete-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "publishable_with_flags"
    assert body["outputs"]["plan_text"] == "# Legacy final plan"


def test_legacy_review_required_hold_row_remains_hidden_from_athlete():
    # A stored review_required row stays held when the release policy still holds
    # it: an unresolved workload/dose error, not merely an admin-review finding.
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="review_required",
            plan_text="",
            final_plan_text="# Held legacy final plan",
            stage2_validator_report={
                "errors": [{"code": "conditioning_role_workload_underfilled"}],
                "warnings": [],
            },
        ),
    )
    response = client.get(f"/api/plans/{plan['id']}", headers={"Authorization": "Bearer athlete-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "held_for_review"
    assert body["outputs"]["plan_text"] == ""


def test_legacy_review_required_admin_blocking_row_releases_with_flags():
    # Findings are observational now: an admin-review-blocking warning no longer
    # holds a stored review_required row back from the athlete.
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="review_required",
            plan_text="",
            final_plan_text="# Legacy final plan",
            stage2_validator_report={"errors": [], "warnings": [{"code": "missing_required_element", "blocking": True}]},
        ),
    )
    response = client.get(f"/api/plans/{plan['id']}", headers={"Authorization": "Bearer athlete-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "publishable_with_flags"
    assert body["outputs"]["plan_text"] == "# Legacy final plan"


def test_legacy_review_required_without_validator_report_stays_held_and_hidden():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="review_required",
            plan_text="",
            final_plan_text="# Legacy text without validator report",
            stage2_validator_report={},
        ),
    )
    response = client.get(f"/api/plans/{plan['id']}", headers={"Authorization": "Bearer athlete-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "held_for_review"
    assert body["outputs"]["plan_text"] == ""


def test_plan_summary_explains_held_for_review_reason():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={})
    store.ensure_profile(athlete)
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            status="review_required",
            plan_text="",
            final_plan_text="# Held final plan",
            stage2_validator_report={
                "errors": [{"code": "conditioning_role_workload_underfilled"}],
                "warnings": [],
            },
        ),
    )

    response = client.get("/api/plans", headers={"Authorization": "Bearer athlete-token"})

    assert response.status_code == 200
    listed_plan = next(item for item in response.json() if item["plan_id"] == plan["id"])
    assert listed_plan["status"] == "held_for_review"
    assert listed_plan["review_reason"] == (
        "Admin review is required before release because Stage 2 validation "
        "found errors: conditioning role workload underfilled."
    )


def test_legacy_rows_with_only_plan_text_remain_readable():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    legacy = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=stage1_result(),
    )

    response = client.get(
        f"/api/plans/{legacy['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )

    assert response.status_code == 200
    assert response.json()["outputs"]["plan_text"] == "# Stage 1 Draft"


def test_plan_detail_returns_public_sparring_advisory_without_changing_saved_plan_text():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    original_text = "# Final Plan\n- Keep the saved plan untouched."
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(
            plan_text=original_text,
            final_plan_text=original_text,
            planning_brief=advisory_planning_brief(
                readiness_flags=["fight_week"],
                injuries=["mild stable shoulder soreness"],
                hard_sparring_days=["Tuesday", "Thursday"],
            ),
        ),
    )

    response = client.get(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outputs"]["plan_text"] == original_text
    assert len(body["advisories"]) == 1
    # days_until_fight=6 falls inside the D-17 → D-0 countdown override, so the
    # planner converts each declared hard sparring day to technical rather than
    # merely deloading it.
    assert body["advisories"][0]["action"] == "convert"
    assert body["advisories"][0]["days"] == ["Tuesday", "Thursday"]
    assert body["advisories"][0]["title"] == "Coach note"
    assert body["advisories"][0]["disclaimer"] == "Treat this as a flag, not an automatic change to your saved plan."
    assert store.get_plan(plan["id"])["plan_text"] == original_text


def test_plan_detail_advisory_is_derived_from_structured_context_not_saved_plan_text():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    planning_brief = advisory_planning_brief(
        readiness_flags=["fight_week", "active_weight_cut"],
        fatigue="high",
        injuries=["worsening ankle instability"],
        weight_cut_pct=5.4,
        hard_sparring_days=["Tuesday", "Thursday"],
    )
    first = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_1",
        request=_build_request(),
        result=finalized_result(
            plan_text="# Final Plan A\n- Preserve this text.",
            final_plan_text="# Final Plan A\n- Preserve this text.",
            planning_brief=planning_brief,
        ),
    )
    second = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_2",
        request=_build_request(),
        result=finalized_result(
            plan_text="# Final Plan B\n- Different saved wording.",
            final_plan_text="# Final Plan B\n- Different saved wording.",
            planning_brief=planning_brief,
        ),
    )

    first_response = client.get(
        f"/api/plans/{first['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )
    second_response = client.get(
        f"/api/plans/{second['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["outputs"]["plan_text"] != second_response.json()["outputs"]["plan_text"]
    assert first_response.json()["advisories"] == second_response.json()["advisories"]


def test_latest_plan_endpoint_does_not_promote_generated_saved_plan():
    client, store, _ = _build_client()

    response = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer athlete-token"},
        json=_build_request().model_dump(mode="json"),
    )

    assert response.status_code == 202
    latest = client.get("/api/plans/latest", headers={"Authorization": "Bearer athlete-token"})

    assert latest.status_code == 404
    assert store.get_active_plan_id("athlete-1") is None


def test_set_active_endpoint_blocks_overlapping_active_plan_until_user_chooses():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    # Anchor both camps to today so the "current" plan stays live (its fight
    # date must not have passed, or the overlap guard treats it as ended and
    # the block never fires). Fixed calendar dates turn this into a time-bomb
    # that flips 409 -> 200 once the wall clock passes the hardcoded fight date.
    today = date.today()
    current_end = today + timedelta(days=25)
    draft_end = today + timedelta(days=33)
    current = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_current",
        request=_build_request({"fight_date": current_end.isoformat()}),
        result=finalized_result(
            structured_plan=_structured_date_range(
                (today - timedelta(days=6)).isoformat(),
                current_end.isoformat(),
            )
        ),
    )
    draft = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_draft",
        request=_build_request({"fight_date": draft_end.isoformat()}),
        result=finalized_result(
            structured_plan=_structured_date_range(
                (today + timedelta(days=2)).isoformat(),
                draft_end.isoformat(),
            )
        ),
    )
    store.set_active_plan_id("athlete-1", current["id"])

    blocked = client.post(
        f"/api/plans/{draft['id']}/set-active",
        headers={"Authorization": "Bearer athlete-token"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "active_plan_overlap",
        "message": "This overlaps with your current active plan. Do you want to replace the current plan, pause it, or choose a new start date?",
    }
    assert store.get_active_plan_id("athlete-1") == current["id"]

    paused = client.post(
        f"/api/plans/{draft['id']}/set-active",
        headers={"Authorization": "Bearer athlete-token"},
        json={"overlap_action": "pause"},
    )

    assert paused.status_code == 200
    assert paused.json()["plan_id"] == draft["id"]
    assert paused.json()["activation_state"] == "eligible"
    assert store.get_active_plan_id("athlete-1") == draft["id"]
    assert store.get_plan(current["id"])["status"] == "ready"


def test_passed_camp_is_completed_history_and_cannot_be_activated():
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
        intake_id="intake_passed",
        request=_build_request(),
        result=finalized_result(),
    )
    plan["fight_date"] = (date.today() - timedelta(days=2)).isoformat()
    store.set_active_plan_id("athlete-1", plan["id"])

    listed = client.get("/api/plans", headers={"Authorization": "Bearer athlete-token"})
    detail = client.get(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )
    active = client.get("/api/plans/active", headers={"Authorization": "Bearer athlete-token"})
    rejected = client.post(
        f"/api/plans/{plan['id']}/set-active",
        headers={"Authorization": "Bearer athlete-token"},
    )

    assert listed.status_code == 200
    assert next(item for item in listed.json() if item["plan_id"] == plan["id"])["activation_state"] == "fight_date_passed"
    assert detail.status_code == 200
    assert detail.json()["activation_state"] == "fight_date_passed"
    assert active.status_code == 404
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {
        "code": "plan_has_ended",
        "message": "This fight camp has ended and cannot be activated.",
        "activation_state": "fight_date_passed",
    }
    assert store.get_active_plan_id("athlete-1") == plan["id"]

    renamed = client.patch(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
        json={"plan_name": "Completed camp"},
    )
    archived = client.delete(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["activation_state"] == "fight_date_passed"
    assert archived.status_code in {200, 204}


def test_activation_reconciles_adherence_without_awarding_xp(monkeypatch):
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
        intake_id="intake_existing",
        request=_build_request(),
        result=finalized_result(),
    )
    calls = []
    monkeypatch.setattr(
        plan_routes,
        "reconcile_adherence_streak",
        lambda _store, **kwargs: calls.append(kwargs),
    )
    awards_before = list(store.xp_awards.get("athlete-1", []))

    response = client.post(
        f"/api/plans/{plan['id']}/set-active",
        headers={"Authorization": "Bearer athlete-token"},
    )

    assert response.status_code == 200
    assert store.get_active_plan_id("athlete-1") == plan["id"]
    assert calls == [{"athlete_id": "athlete-1", "athlete_timezone": ""}]
    assert store.xp_awards.get("athlete-1", []) == awards_before


def test_athlete_can_rename_their_saved_plan():
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

    response = client.patch(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
        json=PlanRenameRequest(plan_name="April Fight Camp").model_dump(mode="json"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["plan_name"] == "April Fight Camp"
    assert store.get_plan(plan["id"])["plan_name"] == "April Fight Camp"


def test_athlete_can_rename_their_saved_plan_via_documented_name_route():
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

    response = client.patch(
        f"/api/plans/{plan['id']}/name",
        headers={"Authorization": "Bearer athlete-token"},
        json=PlanRenameRequest(plan_name="Camp A").model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["plan_name"] == "Camp A"
    assert store.get_plan(plan["id"])["plan_name"] == "Camp A"


def test_archived_plan_is_preview_only_for_athlete_history():
    client, store, _ = _build_client()
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    store.ensure_profile(athlete)
    visible_plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_visible",
        request=_build_request({"fight_date": "2098-05-01"}),
        result=finalized_result(status="ready", plan_text="# Visible", final_plan_text="# Visible"),
    )
    archived_plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_archived",
        request=_build_request({"fight_date": "2099-06-01"}),
        result=finalized_result(
            status="archived",
            plan_text="",
            final_plan_text="# Archived copy",
            stage2_status="admin_archived",
        ),
    )
    store.set_active_plan_id("athlete-1", visible_plan["id"])

    list_response = client.get("/api/plans", headers={"Authorization": "Bearer athlete-token"})
    latest_response = client.get("/api/plans/latest", headers={"Authorization": "Bearer athlete-token"})
    me_response = client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    archived_detail_response = client.get(
        f"/api/plans/{archived_plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )
    admin_detail_response = client.get(
        f"/api/plans/{archived_plan['id']}",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert list_response.status_code == 200
    assert {plan["plan_id"] for plan in list_response.json()} == {archived_plan["id"], visible_plan["id"]}
    assert latest_response.status_code == 200
    assert latest_response.json()["plan_id"] == visible_plan["id"]
    assert me_response.status_code == 200
    assert me_response.json()["latest_plan"]["plan_id"] == visible_plan["id"]
    assert me_response.json()["plan_count"] == 1
    assert archived_detail_response.status_code == 200
    assert archived_detail_response.json()["status"] == "archived"
    assert archived_detail_response.json()["outputs"]["plan_text"] == "# Archived copy"
    assert admin_detail_response.status_code == 200
    assert admin_detail_response.json()["status"] == "archived"


def test_athlete_can_archive_their_saved_plan():
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

    response = client.delete(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )

    assert response.status_code == 204
    archived = store.get_plan(plan["id"])
    assert archived is not None
    assert archived["status"] == "archived"

    list_response = client.get("/api/plans", headers={"Authorization": "Bearer athlete-token"})
    latest_response = client.get("/api/plans/latest", headers={"Authorization": "Bearer athlete-token"})
    detail_response = client.get(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )
    assert list_response.status_code == 200
    assert [item["plan_id"] for item in list_response.json()] == [plan["id"]]
    assert latest_response.status_code == 404
    assert detail_response.status_code == 200
    assert detail_response.json()["status"] == "archived"


@pytest.mark.parametrize("actor", ["athlete-token", "admin-token"])
def test_archiving_active_plan_activates_sole_remaining_and_rebuilds_streaks(monkeypatch, actor):
    client, store, _ = _build_client()
    first = store.create_plan(
        athlete_id="athlete-1", intake_id="intake_x",
        request=_build_request(), result=finalized_result(),
    )
    second = store.create_plan(
        athlete_id="athlete-1", intake_id="intake_x",
        request=_build_request(), result=finalized_result(),
    )
    store.set_active_plan_id("athlete-1", first["id"])

    def conditional(athlete_id, plan_id, expected_id, current_training_day=None):
        if store.get_active_plan_id(athlete_id) != expected_id:
            return False
        remaining = [p for p in store.list_user_plans(athlete_id) if p["status"] != "archived"]
        if len(remaining) != 1 or remaining[0]["id"] != plan_id:
            return False
        store.set_active_plan_id(athlete_id, plan_id)
        return True

    store.try_set_active_plan_id = conditional
    rebuilt = []
    monkeypatch.setattr(plan_routes, "reconcile_training_streak", lambda *a, **k: rebuilt.append("training"))
    monkeypatch.setattr(plan_routes, "reconcile_adherence_streak", lambda *a, **k: rebuilt.append("adherence"))

    response = client.delete(f"/api/plans/{first['id']}", headers={"Authorization": f"Bearer {actor}"})
    assert response.status_code == 204, response.text
    assert store.get_active_plan_id("athlete-1") == second["id"]
    assert rebuilt == ["training", "adherence"]


def test_athlete_cannot_archive_someone_elses_plan():
    client, store, _ = _build_client()
    owner = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    other_user = AuthenticatedUser(
        user_id="athlete-2",
        email="other@example.com",
        full_name="Other Athlete",
        metadata={},
    )
    store.ensure_profile(owner)
    store.ensure_profile(other_user)
    client.app.state.auth_service.users_by_token["other-token"] = other_user
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(),
    )

    response = client.delete(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer other-token"},
    )

    # Athlete-scoped store methods treat another athlete's plan as missing, so
    # the response is 404 (existence is not leaked) rather than 403.
    assert response.status_code == 404
    assert store.get_plan(plan["id"]) is not None


def test_athlete_cannot_rename_someone_elses_plan():
    client, store, _ = _build_client()
    owner = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    other_user = AuthenticatedUser(
        user_id="athlete-2",
        email="other@example.com",
        full_name="Other Athlete",
        metadata={},
    )
    store.ensure_profile(owner)
    store.ensure_profile(other_user)
    client.app.state.auth_service.users_by_token["other-token"] = other_user
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(),
    )

    response = client.patch(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer other-token"},
        json={"plan_name": "hijacked"},
    )

    # Scoped rename does not leak existence: 404, and the name is unchanged.
    assert response.status_code == 404
    assert store.get_plan(plan["id"])["plan_name"] == ""


def test_admin_delete_archives_plan_without_hard_delete():
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

    response = client.delete(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 204
    # The user-facing delete is archive-only: the row must survive, archived.
    archived = store.get_plan(plan["id"])
    assert archived is not None
    assert archived["status"] == "archived"


def test_delete_plan_returns_409_when_generation_job_active():
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
        client_request_id="active-delete-guard",
        source="web_intake",
        request_payload=_build_request().model_dump(mode="json"),
        plan_id=plan["id"],
        intake_id="intake_x",
    )
    store.update_generation_job(job["id"], status="running", started_at="2026-01-01T00:00:00+00:00")

    response = client.delete(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Plan has an active generation job. Cancel or wait before archiving."
    assert store.get_plan(plan["id"]) is not None


def test_athlete_repeated_delete_is_idempotent_archive():
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

    first_delete = client.delete(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )
    second_delete = client.delete(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )

    assert first_delete.status_code == 204
    # A second delete on an already-archived plan is an idempotent no-op,
    # never a hard delete.
    assert second_delete.status_code == 204
    archived = store.get_plan(plan["id"])
    assert archived is not None
    assert archived["status"] == "archived"


def test_athlete_cannot_rename_archived_plan():
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
        result=finalized_result(status="archived", stage2_status="admin_archived"),
    )

    response = client.patch(
        f"/api/plans/{plan['id']}",
        headers={"Authorization": "Bearer athlete-token"},
        json={"plan_name": "Renamed"},
    )

    assert response.status_code == 404


def test_generation_job_endpoint_requires_same_athlete_or_admin():
    client, _, _ = _build_client()
    response = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer athlete-token"},
        json=_build_request().model_dump(mode="json"),
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    other_user = AuthenticatedUser(
        user_id="athlete-2",
        email="other@example.com",
        full_name="Other Athlete",
        metadata={},
    )
    app_store = client.app.state.store
    client.app.state.auth_service.users_by_token["other-token"] = other_user
    app_store.ensure_profile(other_user)

    forbidden = client.get(
        f"/api/generation-jobs/{job_id}",
        headers={"Authorization": "Bearer other-token"},
    )
    allowed = client.get(
        f"/api/generation-jobs/{job_id}",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert forbidden.status_code == 403
    assert allowed.status_code == 200


def test_generation_job_endpoint_rejects_malformed_job_id_before_store_lookup():
    client, store, _ = _build_client()
    store.get_generation_job = MagicMock(side_effect=AssertionError("store should not be called"))  # type: ignore[method-assign]

    response = client.get(
        "/api/generation-jobs/not-a-generation-job",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "generation job not found"
    assert response.json()["code"] == "generation_job_not_found"
    store.get_generation_job.assert_not_called()


def test_generation_job_id_validation_handles_non_string_ids_cleanly():
    with pytest.raises(HTTPException) as exc_info:
        _validate_generation_job_id(None)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "generation job not found"


def test_admin_can_list_athlete_generation_jobs_with_sanitized_summary_and_retry_flags():
    client, store, _ = _build_client()
    store.ensure_profile(client.app.state.auth_service.users_by_token["athlete-token"])
    request_payload = _build_request().model_dump(mode="json")
    request_payload["api_key"] = "should-not-return"
    warning = "Profile refresh failed; plan generated from submitted intake only."
    failed = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="cli_failed",
        source="admin_latest_intake",
        request_payload=request_payload,
    )
    store.update_generation_job(
        failed["id"],
        status="failed",
        error="Stage 2 timeout",
        completed_at="2026-05-01T12:00:00+00:00",
        progress_milestones=[
            {"code": "profile_refresh_failed_warning", "detail": warning, "meta": {"warning": True}},
        ],
    )
    response = client.get(
        "/api/admin/athletes/athlete-1/generation-jobs",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) >= 1
    first = body[0]
    assert first["status"] in {"failed", "queued", "running", "completed"}
    assert "created_at" in first
    assert "source" in first
    assert first["error"] == "Stage 2 timeout"
    assert first["client_request_id"] == "cli_failed"
    assert first["can_retry"] is True
    assert first["warnings"] == [warning]
    summary = first["request_payload_summary"]
    assert summary["athlete_name"] == request_payload["athlete"]["full_name"]
    assert summary["fight_date"] == request_payload["fight_date"]
    assert summary["fight_format"] == request_payload["rounds_format"]
    assert summary["fatigue_level"] == request_payload["fatigue_level"]
    assert summary["goals"] == request_payload["key_goals"]
    assert summary["weaknesses"] == request_payload["weak_areas"]
    assert summary["injuries"]
    assert summary["training_availability"] == ", ".join(request_payload["training_availability"])
    assert "api_key" not in summary
    assert "token" not in summary
    assert "authorization" not in summary
    assert "supabase_key" not in summary


def test_non_admin_cannot_list_admin_generation_jobs_and_stale_job_is_flagged():
    client, store, _ = _build_client()
    store.ensure_profile(client.app.state.auth_service.users_by_token["athlete-token"])
    job = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="retry_old_job_123",
        source="self_serve",
        request_payload=_build_request().model_dump(mode="json"),
    )
    store.update_generation_job(
        job["id"],
        status="running",
        started_at="2026-01-01T00:00:00+00:00",
        heartbeat_at="2026-01-01T00:00:00+00:00",
    )
    forbidden = client.get(
        "/api/admin/athletes/athlete-1/generation-jobs",
        headers={"Authorization": "Bearer athlete-token"},
    )
    assert forbidden.status_code == 403
    allowed = client.get(
        "/api/admin/athletes/athlete-1/generation-jobs",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert allowed.status_code == 200
    stale = allowed.json()[0]
    assert stale["is_stale"] is True
    assert stale["stale_reason"]
    assert stale["retry_of"] == "old_job"


def test_admin_generation_jobs_normalizes_legacy_ready_status():
    client, store, _ = _build_client()
    store.ensure_profile(client.app.state.auth_service.users_by_token["athlete-token"])
    job = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="legacy_ready",
        source="self_serve",
        request_payload=_build_request().model_dump(mode="json"),
    )
    store.generation_jobs[job["id"]].update(
        {"status": "ready", "completed_at": "2026-01-01T00:00:00+00:00"}
    )

    response = client.get(
        "/api/admin/athletes/athlete-1/generation-jobs",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert response.status_code == 200
    assert response.json()[0]["status"] == "completed"


def test_plan_completions_returns_own_rows_and_current_training_day():
    client, store, _ = _build_client()
    store.ensure_profile(client.app.state.auth_service.users_by_token["athlete-token"])
    plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(),
    )
    other_plan = store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake_y",
        request=_build_request(),
        result=finalized_result(),
    )
    store.upsert_session_completion(
        "athlete-1",
        {
            "plan_id": plan["id"],
            "session_id": "s1",
            "training_day": "2026-06-01",
            "status": "done",
            "session_rpe": 8,
            "started_at": "2026-06-01T10:00:00+00:00",
            "completed_at": "2026-06-01T11:00:00+00:00",
        },
    )
    # A row on a different plan must not leak into this plan's view.
    store.upsert_session_completion(
        "athlete-1",
        {
            "plan_id": other_plan["id"],
            "session_id": "sx",
            "training_day": "2026-06-02",
            "status": "skipped",
            "modification_reason": "travel",
        },
    )

    response = client.get(
        f"/api/plans/{plan['id']}/completions",
        headers={"Authorization": "Bearer athlete-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert [row["session_id"] for row in body["completions"]] == ["s1"]
    assert body["completions"][0]["status"] == "done"
    assert body["completions"][0]["session_rpe"] == 8
    # Server-authoritative athlete-local day, ISO formatted.
    assert len(body["current_training_day"]) == 10


def test_plan_completions_rejects_another_athletes_plan():
    client, store, _ = _build_client()
    other_user = AuthenticatedUser(
        user_id="athlete-2",
        email="other@example.com",
        full_name="Other Athlete",
        metadata={},
    )
    store.ensure_profile(other_user)
    plan = store.create_plan(
        athlete_id="athlete-2",
        intake_id="intake_x",
        request=_build_request(),
        result=finalized_result(),
    )
    response = client.get(
        f"/api/plans/{plan['id']}/completions",
        headers={"Authorization": "Bearer athlete-token"},
    )
    assert response.status_code == 404
