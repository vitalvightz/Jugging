from __future__ import annotations

from datetime import date, timedelta

from fastapi import HTTPException, status

from api.contracts.training_day import current_training_day
from support import _build_client, _build_request


def _get_current_workspace(client) -> dict:
    response = client.get("/api/nutrition/current", headers={"Authorization": "Bearer athlete-token"})
    assert response.status_code == 200
    return response.json()


def test_nutrition_workspace_update_adds_visible_sparring_days_to_hidden_training_availability():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    store.create_intake(
        "athlete-1",
        _build_request(
            {
                "training_availability": ["Monday", "Wednesday"],
                "weekly_training_frequency": 2,
                "hard_sparring_days": ["Wednesday"],
        "support_work_days": [],
            }
        ),
    )

    workspace = _get_current_workspace(client)
    update = {
        "nutrition_profile": workspace["nutrition_profile"],
        "shared_camp_context": {
            **workspace["shared_camp_context"],
            "training_availability": [],
            "hard_sparring_days": ["Friday"],
        "support_work_days": ["Tuesday"],
        },
        "s_and_c_preferences": workspace["s_and_c_preferences"],
        "nutrition_readiness": workspace["nutrition_readiness"],
        "nutrition_monitoring": workspace["nutrition_monitoring"],
        "nutrition_coach_controls": workspace["nutrition_coach_controls"],
    }

    response = client.put(
        "/api/nutrition/current",
        headers={"Authorization": "Bearer athlete-token"},
        json=update,
    )

    assert response.status_code == 200
    shared = response.json()["shared_camp_context"]
    assert shared["training_availability"] == ["Monday", "Wednesday", "Friday", "Tuesday"]
    assert shared["session_types_by_day"]["friday"] == "hard_spar"
    assert shared["session_types_by_day"]["tuesday"] == "technical"


def test_nutrition_workspace_missing_required_fields_do_not_include_training_availability():
    client, _, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})

    workspace = _get_current_workspace(client)

    assert "training_availability" not in workspace["derived"]["missing_required_fields"]


def test_nutrition_workspace_rejects_overlap_between_hard_sparring_and_support_work_days():
    client, _, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    workspace = _get_current_workspace(client)
    update = {
        "nutrition_profile": workspace["nutrition_profile"],
        "shared_camp_context": {
            **workspace["shared_camp_context"],
            "training_availability": ["Tuesday"],
            "hard_sparring_days": ["Tuesday"],
            "support_work_days": ["Tuesday"],
            "session_types_by_day": {"tuesday": "hard_spar"},
        },
        "s_and_c_preferences": workspace["s_and_c_preferences"],
        "nutrition_readiness": workspace["nutrition_readiness"],
        "nutrition_monitoring": workspace["nutrition_monitoring"],
        "nutrition_coach_controls": workspace["nutrition_coach_controls"],
    }

    response = client.put(
        "/api/nutrition/current",
        headers={"Authorization": "Bearer athlete-token"},
        json=update,
    )

    assert response.status_code == 422
    assert "cannot be both hard_sparring and support_work" in response.json()["detail"].lower()


def test_nutrition_workspace_update_retries_profile_update_without_nutrition_profile():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    store.create_intake("athlete-1", _build_request({}))

    original_update_profile = store.update_profile
    update_calls: list[dict] = []

    def flaky_update_profile(athlete_id, update):
        payload = update.model_dump(mode="json", exclude_none=True)
        update_calls.append(payload)
        if payload.get("nutrition_profile") is not None:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="failed to update profile")
        return original_update_profile(athlete_id, update)

    store.update_profile = flaky_update_profile  # type: ignore[assignment]

    workspace = _get_current_workspace(client)
    update = {
        "nutrition_profile": workspace["nutrition_profile"],
        "shared_camp_context": workspace["shared_camp_context"],
        "s_and_c_preferences": workspace["s_and_c_preferences"],
        "nutrition_readiness": workspace["nutrition_readiness"],
        "nutrition_monitoring": {
            "daily_bodyweight_log": [
                {"date": "2026-04-11", "weight_kg": 72.4, "time": "07:00", "is_fasted": True, "notes": "am"},
            ],
        },
        "nutrition_coach_controls": workspace["nutrition_coach_controls"],
    }

    response = client.put(
        "/api/nutrition/current",
        headers={"Authorization": "Bearer athlete-token"},
        json=update,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["nutrition_monitoring"]["daily_bodyweight_log"][0]["weight_kg"] == 72.4
    assert len(update_calls) == 2
    assert "nutrition_profile" in update_calls[0]
    assert "nutrition_profile" not in update_calls[1]


def test_nutrition_workspace_update_retries_profile_update_without_nutrition_profile_for_any_5xx_detail():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    store.create_intake("athlete-1", _build_request({}))

    original_update_profile = store.update_profile
    update_calls: list[dict] = []

    def flaky_update_profile(athlete_id, update):
        payload = update.model_dump(mode="json", exclude_none=True)
        update_calls.append(payload)
        if payload.get("nutrition_profile") is not None:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="database write failed")
        return original_update_profile(athlete_id, update)

    store.update_profile = flaky_update_profile  # type: ignore[assignment]

    workspace = _get_current_workspace(client)
    update = {
        "nutrition_profile": workspace["nutrition_profile"],
        "shared_camp_context": workspace["shared_camp_context"],
        "s_and_c_preferences": workspace["s_and_c_preferences"],
        "nutrition_readiness": workspace["nutrition_readiness"],
        "nutrition_monitoring": {
            "daily_bodyweight_log": [
                {"date": "2026-04-12", "weight_kg": 71.9, "time": "07:05", "is_fasted": True, "notes": "am"},
            ],
        },
        "nutrition_coach_controls": workspace["nutrition_coach_controls"],
    }

    response = client.put(
        "/api/nutrition/current",
        headers={"Authorization": "Bearer athlete-token"},
        json=update,
    )

    assert response.status_code == 200
    assert len(update_calls) == 2
    assert "nutrition_profile" in update_calls[0]
    assert "nutrition_profile" not in update_calls[1]


def test_athlete_nutrition_workspace_update_ignores_coach_control_fields():
    client, _, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})

    workspace = _get_current_workspace(client)
    update = {
        "nutrition_profile": workspace["nutrition_profile"],
        "shared_camp_context": workspace["shared_camp_context"],
        "s_and_c_preferences": workspace["s_and_c_preferences"],
        "nutrition_readiness": workspace["nutrition_readiness"],
        "nutrition_monitoring": workspace["nutrition_monitoring"],
        "nutrition_coach_controls": {
            **workspace["nutrition_coach_controls"],
            "coach_override_enabled": True,
            "do_not_reduce_below_calories": 2400,
            "protein_floor_g_per_kg": 2.2,
        },
    }

    response = client.put(
        "/api/nutrition/current",
        headers={"Authorization": "Bearer athlete-token"},
        json=update,
    )

    assert response.status_code == 200
    controls = response.json()["nutrition_coach_controls"]
    assert controls == workspace["nutrition_coach_controls"]


def test_admin_nutrition_workspace_update_allows_coach_control_fields():
    client, _, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    client.get("/api/me", headers={"Authorization": "Bearer admin-token"})

    get_response = client.get(
        "/api/admin/athletes/athlete-1/nutrition/current",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert get_response.status_code == 200
    workspace = get_response.json()
    update = {
        "nutrition_profile": workspace["nutrition_profile"],
        "shared_camp_context": workspace["shared_camp_context"],
        "s_and_c_preferences": workspace["s_and_c_preferences"],
        "nutrition_readiness": workspace["nutrition_readiness"],
        "nutrition_monitoring": workspace["nutrition_monitoring"],
        "nutrition_coach_controls": {
            **workspace["nutrition_coach_controls"],
            "coach_override_enabled": True,
            "athlete_override_enabled": True,
            "do_not_reduce_below_calories": 2500,
            "protein_floor_g_per_kg": 2.0,
            "fight_week_manual_mode": True,
            "water_cut_locked_to_manual": True,
        },
    }

    response = client.put(
        "/api/admin/athletes/athlete-1/nutrition/current",
        headers={"Authorization": "Bearer admin-token"},
        json=update,
    )

    assert response.status_code == 200
    controls = response.json()["nutrition_coach_controls"]
    assert controls["coach_override_enabled"] is True
    assert controls["athlete_override_enabled"] is True
    assert controls["do_not_reduce_below_calories"] == 2500
    assert controls["protein_floor_g_per_kg"] == 2.0
    assert controls["fight_week_manual_mode"] is True
    assert controls["water_cut_locked_to_manual"] is True


def _put_bodyweight_log(client, workspace: dict, entries: list[dict]) -> dict:
    response = client.put(
        "/api/nutrition/current",
        headers={"Authorization": "Bearer athlete-token"},
        json={
            "nutrition_profile": workspace["nutrition_profile"],
            "shared_camp_context": workspace["shared_camp_context"],
            "s_and_c_preferences": workspace["s_and_c_preferences"],
            "nutrition_readiness": workspace["nutrition_readiness"],
            "nutrition_monitoring": {"daily_bodyweight_log": entries},
            "nutrition_coach_controls": workspace["nutrition_coach_controls"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _intake_weight(store) -> float:
    return store.get_latest_intake("athlete-1")["intake"]["athlete"]["weight_kg"]


def _days_from_today(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def test_weight_tracker_days_follow_active_plan_instead_of_latest_intake():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    active_request = _build_request({"fight_date": _days_from_today(20)})
    active_intake = store.create_intake("athlete-1", active_request)
    active_plan = store.create_plan(
        athlete_id="athlete-1", intake_id=active_intake["id"],
        request=active_request, result={"status": "ready"},
    )
    store.set_active_plan_id("athlete-1", active_plan["id"])
    store.create_intake("athlete-1", _build_request({"fight_date": _days_from_today(1)}))

    workspace = _get_current_workspace(client)
    expected_days = (date.fromisoformat(active_plan["fight_date"]) - current_training_day(
        athlete_timezone=store.profiles["athlete-1"].get("athlete_timezone"),
    )).days
    assert workspace["derived"]["days_until_fight"] == expected_days
    assert workspace["shared_camp_context"]["fight_date"] == _days_from_today(1)

    response = client.put(
        "/api/nutrition/current",
        headers={"Authorization": "Bearer athlete-token"},
        json=workspace,
    )
    assert response.status_code == 200, response.text
    assert response.json()["derived"]["days_until_fight"] == expected_days
    assert store.get_latest_intake("athlete-1")["intake"]["fight_date"] == _days_from_today(1)


MONDAY = {"date": _days_from_today(-4), "weight_kg": 71.8, "time": "07:00", "is_fasted": True, "notes": None}
WEDNESDAY = {"date": _days_from_today(-2), "weight_kg": 71.2, "time": "07:05", "is_fasted": True, "notes": None}


def test_a_new_weigh_in_becomes_the_camp_current_weight():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    store.create_intake("athlete-1", _build_request({}))
    assert _intake_weight(store) == 72.5

    workspace = _put_bodyweight_log(client, _get_current_workspace(client), [MONDAY])

    assert workspace["shared_camp_context"]["current_weight_kg"] == 71.8
    assert workspace["shared_camp_context"]["current_weight_source"] == "latest_bodyweight_log"
    assert workspace["shared_camp_context"]["current_weight_recorded_at"] == f"{MONDAY['date']}T07:00"
    # Camp Setup, the next plan and the cut maths read the stored intake weight.
    assert _intake_weight(store) == 71.8
    assert workspace["derived"]["weight_cut_pct"] == 2.5


def test_editing_or_deleting_the_newest_weigh_in_moves_the_current_weight():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    store.create_intake("athlete-1", _build_request({}))
    workspace = _put_bodyweight_log(client, _get_current_workspace(client), [MONDAY, WEDNESDAY])
    assert _intake_weight(store) == 71.2

    workspace = _put_bodyweight_log(client, workspace, [MONDAY, {**WEDNESDAY, "weight_kg": 71.4}])
    assert _intake_weight(store) == 71.4

    workspace = _put_bodyweight_log(client, workspace, [MONDAY])
    assert _intake_weight(store) == 71.8
    assert workspace["shared_camp_context"]["current_weight_kg"] == 71.8

    # An emptied log keeps the last known weight rather than clearing it.
    workspace = _put_bodyweight_log(client, workspace, [])
    assert _intake_weight(store) == 71.8


def test_tidying_older_weigh_ins_keeps_a_weight_set_later_in_camp_setup():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    store.create_intake("athlete-1", _build_request({}))
    _put_bodyweight_log(client, _get_current_workspace(client), [MONDAY, WEDNESDAY])

    intake = store.get_latest_intake("athlete-1")
    camp_setup = {**intake["intake"], "athlete": {**intake["intake"]["athlete"], "weight_kg": 70.9}}
    store.update_intake(intake["id"], intake=camp_setup, fight_date=intake["fight_date"], technical_style=["boxing"])

    workspace = _get_current_workspace(client)
    assert workspace["shared_camp_context"]["current_weight_kg"] == 70.9
    _put_bodyweight_log(client, workspace, [{**MONDAY, "notes": "after travel"}, WEDNESDAY])

    assert _intake_weight(store) == 70.9


def test_workspace_reports_the_canonical_weight_cut_band_for_adults():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    store.create_intake(
        "athlete-1",
        _build_request({"athlete": {**_build_request({}).athlete.model_dump(mode="json"), "weight_kg": 80.0, "target_weight_kg": 74.0}}),
    )

    derived = _get_current_workspace(client)["derived"]

    assert derived["weight_cut_pct"] == 7.5
    assert derived["weight_cut_risk_band"] == "severe"
    assert derived["weight_cut_supervision_required"] is True



def test_a_future_dated_weigh_in_is_rejected():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    store.create_intake("athlete-1", _build_request({}))
    workspace = _get_current_workspace(client)

    response = client.put(
        "/api/nutrition/current",
        headers={"Authorization": "Bearer athlete-token"},
        json={**workspace, "nutrition_monitoring": {"daily_bodyweight_log": [
            {"date": _days_from_today(5), "weight_kg": 69.0, "time": "07:00", "is_fasted": True, "notes": None},
        ]}},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "A weight can't be dated in the future."
    assert _intake_weight(store) == 72.5


def test_a_stored_future_weigh_in_never_sets_camp_weight_or_blocks_saves():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    future = {"date": _days_from_today(5), "weight_kg": 69.0, "time": "07:00", "is_fasted": True, "notes": None}
    # Saved before the date check existed.
    store.create_intake("athlete-1", _build_request({}))
    intake = store.get_latest_intake("athlete-1")
    store.update_intake(
        intake["id"],
        intake={**intake["intake"], "nutrition_monitoring": {"daily_bodyweight_log": [future]}},
        fight_date=intake["fight_date"],
        technical_style=["boxing"],
    )
    workspace = _get_current_workspace(client)
    assert workspace["derived"]["bodyweight_trend"] == []

    yesterday = {"date": _days_from_today(-1), "weight_kg": 71.6, "time": "07:00", "is_fasted": True, "notes": None}
    workspace = _put_bodyweight_log(client, workspace, [future, yesterday])

    assert _intake_weight(store) == 71.6
    assert workspace["shared_camp_context"]["current_weight_kg"] == 71.6
    assert [point["scale_weight_kg"] for point in workspace["derived"]["bodyweight_trend"]] == [71.6]


def test_legacy_session_frequency_does_not_break_nutrition_reads_or_change_source():
    from api.models import NutritionMonitoringInput
    from api.nutrition_workspace import _build_shared_camp_context

    saved = {"weekly_training_frequency": 10, "training_availability": ["Monday", "Wednesday"]}
    shared = _build_shared_camp_context(saved, NutritionMonitoringInput())
    assert shared.weekly_training_frequency is None
    assert shared.training_availability == ["Monday", "Wednesday"]
    assert saved["weekly_training_frequency"] == 10


def test_workspace_reports_weight_to_target_status_for_the_fight():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    store.create_intake(
        "athlete-1",
        _build_request({
            "fight_date": _days_from_today(7),
            "athlete": {**_build_request({}).athlete.model_dump(mode="json"), "weight_kg": 73.3, "target_weight_kg": 70.0},
        }),
    )

    derived = _get_current_workspace(client)["derived"]
    assert derived["weight_cut_status"] == "behind"
    assert derived["kg_to_target"] == 3.3
    assert derived["weight_cut_behind_kg"] > 0

    # A lighter weigh-in becomes the camp weight and the status recovers.
    workspace = _put_bodyweight_log(
        client, _get_current_workspace(client),
        [{"date": _days_from_today(0), "weight_kg": 72.6, "time": "07:00", "is_fasted": True, "notes": None}],
    )
    assert workspace["derived"]["weight_cut_status"] == "on_track"
    assert workspace["derived"]["kg_to_target"] == 2.6
    assert workspace["derived"]["weight_cut_behind_kg"] is None


def test_target_and_weigh_in_can_be_added_from_the_tracker_and_the_newest_target_wins():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    athlete = {**_build_request({}).athlete.model_dump(mode="json"), "target_weight_kg": None}
    store.create_intake("athlete-1", _build_request({"athlete": athlete}))
    workspace = _get_current_workspace(client)
    assert workspace["shared_camp_context"]["target_weight_kg"] is None

    def put_target(workspace, target):
        response = client.put(
            "/api/nutrition/current",
            headers={"Authorization": "Bearer athlete-token"},
            json={**workspace, "shared_camp_context": {
                **workspace["shared_camp_context"],
                "target_weight_kg": target, "weigh_in_type": "day_before", "weigh_in_time": "18:00",
            }},
        )
        assert response.status_code == 200, response.text
        return response.json()

    workspace = put_target(workspace, 70.0)
    shared = workspace["shared_camp_context"]
    assert (shared["target_weight_kg"], shared["weigh_in_type"], shared["weigh_in_time"]) == (70.0, "day_before", "18:00")
    assert store.get_latest_intake("athlete-1")["intake"]["athlete"]["target_weight_kg"] == 70.0

    # The newest target wins, even once the intake holds one.
    workspace = put_target(workspace, 66.0)
    assert workspace["shared_camp_context"]["target_weight_kg"] == 66.0
    assert store.get_latest_intake("athlete-1")["intake"]["athlete"]["target_weight_kg"] == 66.0
