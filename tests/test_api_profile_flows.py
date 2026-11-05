from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import pytest

from api.auth import AuthenticatedUser
from api.compliance import age_years
from api.models import ProfileUpdateRequest
from support import DEFAULT_ADULT_DATE_OF_BIRTH, _build_client, _build_request, finalized_result


@pytest.mark.parametrize("raw_status", ["pending", None, "missing"])
def test_new_account_can_use_app_without_admin_approval(raw_status):
    client, store, _ = _build_client()
    if raw_status == "missing":
        store.profiles["athlete-1"].pop("access_status")
    else:
        store.profiles["athlete-1"]["access_status"] = raw_status

    allowed = client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})

    assert allowed.status_code == 200

    removed = client.post(
        "/api/admin/athletes/athlete-1/approve",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert removed.status_code in (404, 405)


def test_admin_athlete_profile_includes_latest_intake_details():
    client, store, _ = _build_client()

    response = client.put(
        "/api/me",
        headers={"Authorization": "Bearer athlete-token"},
        json={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": ["pressure_fighter"],
            "stance": "orthodox",
            "professional_status": "amateur",
            "record": "5-1",
            "athlete_timezone": "Europe/London",
            "athlete_locale": "en-GB",
        },
    )
    assert response.status_code == 200

    generate_response = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer athlete-token"},
        json={
            "athlete": {
                "full_name": "Ari Mensah",
                "age": 29,
                "height_cm": 178,
                "weight_kg": 74,
                "target_weight_kg": 72,
                "technical_style": ["boxing"],
                "tactical_style": ["pressure_fighter"],
                "stance": "orthodox",
                "professional_status": "amateur",
                "record": "5-1",
                "athlete_timezone": "Europe/London",
                "athlete_locale": "en-GB",
            },
            "fight_date": (date.today() + timedelta(days=42)).isoformat(),
            "rounds_format": "3 x 3",
            "weekly_training_frequency": 5,
            "fatigue_level": "moderate",
            "equipment_access": ["heavy_bag", "weights"],
            "training_availability": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
            "hard_sparring_days": ["Friday"],
        "support_work_days": ["Tuesday"],
            "injuries": "Left shoulder management",
            "key_goals": ["conditioning", "fight_sharpness"],
            "weak_areas": ["defense", "gas_tank"],
            "training_preference": "Short, intense pads and bag rounds.",
            "mindset_challenges": "Starts too fast in the first round.",
            "notes": "Loved reactive defense work in the last camp.",
        },
    )
    assert generate_response.status_code == 202

    admin_response = client.get(
        "/api/admin/athletes/athlete-1",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert admin_response.status_code == 200
    payload = admin_response.json()
    assert payload["technical_style"] == ["boxing"]
    assert payload["tactical_style"] == ["pressure_fighter"]
    assert payload["stance"] == "orthodox"
    assert payload["professional_status"] == "amateur"
    assert payload["record"] == "5-1"
    assert payload["athlete_locale"] == "en-GB"
    # The API derives age from the stored date of birth, not from the submitted
    # value, so the expectation must track the clock rather than a literal.
    assert payload["latest_intake"]["athlete"]["age"] == age_years(DEFAULT_ADULT_DATE_OF_BIRTH)
    assert payload["latest_intake"]["equipment_access"] == ["heavy_bag", "weights"]
    assert payload["latest_intake"]["training_preference"] == "Short, intense pads and bag rounds."


def test_admin_can_generate_new_plan_from_latest_intake():
    client, store, _ = _build_client()
    store.create_intake("athlete-1", _build_request())

    response = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 202
    assert response.json()["athlete_id"] == "athlete-1"


def test_admin_athlete_account_can_generate_from_own_latest_intake_via_admin_route():
    client, store, _ = _build_client()
    store.create_intake("admin-1", _build_request())

    response = client.post(
        "/api/admin/athletes/admin-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 202
    job = next(iter(store.generation_jobs.values()))
    assert job["source"] == "admin_latest_intake"
    assert job["athlete_id"] == "admin-1"


def test_admin_generate_uses_selected_athlete_latest_intake_not_admin_draft():
    client, store, _ = _build_client()
    admin_request = _build_request({"athlete": {"full_name": "Admin Name", "technical_style": ["mma"]}})
    store.update_profile("admin-1", ProfileUpdateRequest(onboarding_draft=admin_request.model_dump(mode="json")))

    athlete_request = _build_request({"athlete": {"full_name": "Athlete One", "technical_style": ["boxing"]}})
    athlete_intake = store.create_intake("athlete-1", athlete_request)

    response = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert response.status_code == 202

    job = next(iter(store.generation_jobs.values()))
    assert job["athlete_id"] == "athlete-1"
    assert job["intake_id"] == athlete_intake["id"]
    assert job["source"] == "admin_latest_intake"
    assert job["request_payload"]["athlete"]["full_name"] == "Athlete One"
    assert job["request_payload"]["athlete"]["full_name"] != "Admin Name"

    plan = next(iter(store.plans.values()))
    assert plan["athlete_id"] == "athlete-1"


def test_admin_generate_from_latest_intake_rejects_mismatched_intake_athlete():
    client, store, _ = _build_client()
    store.intakes["athlete-1"] = [
        {
            "id": "intake_bad_link",
            "athlete_id": "athlete-2",
            "fight_date": "2099-01-01",
            "technical_style": ["boxing"],
            "intake": _build_request().model_dump(mode="json"),
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]

    response = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "latest intake belongs to a different athlete"


def test_admin_generate_rejects_existing_job_with_self_serve_source():
    client, store, _ = _build_client()
    latest_intake = store.create_intake("athlete-1", _build_request())
    store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="admin-linkage-1",
        source="self_serve",
        request_payload=_build_request().model_dump(mode="json"),
        intake_id=latest_intake["id"],
    )

    response = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token", "X-Client-Request-Id": "admin-linkage-1"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "unsafe existing admin generation job linkage"


def test_admin_self_serve_on_protected_triage_intake_returns_protected_guidance_without_new_job():
    client, store, _ = _build_client()
    request = _build_request()
    intake = store.create_intake("admin-1", request)
    plan = store.create_plan(
        athlete_id="admin-1",
        intake_id=intake["id"],
        request=request,
        result=finalized_result(status="triage_blocked", stage2_status="needs_review"),
    )

    response = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer admin-token"},
        json=request.model_dump(mode="json") | {"intake_id": intake["id"]},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["plan_id"] == plan["id"]
    assert payload["requires_admin_resume"] is True
    assert payload["stage2_status"] == "needs_review"
    assert "cannot bypass triage" in payload["message"]
    assert store.list_generation_jobs_for_athlete("admin-1", limit=25) == []


def test_athlete_self_serve_on_protected_triage_intake_does_not_get_admin_protected_response():
    client, store, _ = _build_client()
    request = _build_request()
    intake = store.create_intake("athlete-1", request)
    store.create_plan(
        athlete_id="athlete-1",
        intake_id=intake["id"],
        request=request,
        result=finalized_result(status="triage_blocked", stage2_status="needs_review"),
    )

    response = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer athlete-token"},
        json=request.model_dump(mode="json") | {"intake_id": intake["id"]},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["requires_admin_resume"] is False
    assert "cannot bypass triage" not in (payload.get("message") or "").lower()
    assert not str(payload["job_id"]).startswith("protected_")
    assert len(store.list_generation_jobs_for_athlete("athlete-1", limit=25)) == 1


def test_admin_generate_rejects_existing_admin_job_with_wrong_intake_id():
    client, store, _ = _build_client()
    latest_intake = store.create_intake("athlete-1", _build_request())
    wrong_intake = store.create_intake("athlete-1", _build_request({"athlete": {"full_name": "Wrong Intake"}}))
    store.intakes["athlete-1"] = [wrong_intake, latest_intake]
    store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="admin-linkage-2",
        source="admin_latest_intake",
        request_payload=latest_intake["intake"],
        intake_id=wrong_intake["id"],
    )

    response = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token", "X-Client-Request-Id": "admin-linkage-2"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "unsafe existing admin generation job linkage"


def test_admin_generate_rejects_existing_admin_job_with_mismatched_payload():
    client, store, _ = _build_client()
    latest_intake = store.create_intake("athlete-1", _build_request())
    store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="admin-linkage-3",
        source="admin_latest_intake",
        request_payload=_build_request({"athlete": {"full_name": "Bad Payload"}}).model_dump(mode="json"),
        intake_id=latest_intake["id"],
    )

    response = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token", "X-Client-Request-Id": "admin-linkage-3"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "unsafe existing admin generation job linkage"


def test_admin_generate_resets_stale_existing_job_to_admin_latest_intake_linkage():
    client, store, _ = _build_client()
    latest_intake = store.create_intake("athlete-1", _build_request({"athlete": {"full_name": "Latest Name"}}))
    existing_job = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="admin-linkage-4",
        source="self_serve",
        request_payload=_build_request({"athlete": {"full_name": "Old Name"}}).model_dump(mode="json"),
        intake_id=None,
    )
    store.update_generation_job(
        existing_job["id"],
        status="running",
        started_at="2020-01-01T00:00:00+00:00",
        heartbeat_at="2020-01-01T00:00:00+00:00",
    )

    response = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token", "X-Client-Request-Id": "admin-linkage-4"},
    )
    assert response.status_code == 202
    job = store.get_generation_job(response.json()["job_id"])
    assert job is not None
    assert job["source"] == "admin_latest_intake"
    assert job["intake_id"] == latest_intake["id"]
    assert job["request_payload"]["athlete"]["full_name"] == "Latest Name"


def test_admin_generation_does_not_consume_self_serve_daily_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_PLAN_GENERATE_DAILY_LIMIT_PER_USER", "1")
    client, _, _ = _build_client(enable_in_process_generation=False)

    first = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer athlete-token", "X-Client-Request-Id": "self-serve-1"},
        json=_build_request().model_dump(mode="json"),
    )
    assert first.status_code == 202

    admin = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token", "X-Client-Request-Id": "admin-1"},
    )
    assert admin.status_code == 409

    retry_same = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer athlete-token", "X-Client-Request-Id": "self-serve-1"},
        json=_build_request().model_dump(mode="json"),
    )
    assert retry_same.status_code == 202

    second_new = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer athlete-token", "X-Client-Request-Id": "self-serve-2"},
        json=_build_request().model_dump(mode="json"),
    )
    assert second_new.status_code == 409


def test_self_serve_generation_rejects_new_job_when_another_job_is_active():
    client, _, _ = _build_client(enable_in_process_generation=False)
    first = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer athlete-token", "X-Client-Request-Id": "active-1"},
        json=_build_request().model_dump(mode="json"),
    )
    assert first.status_code == 202

    second = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer athlete-token", "X-Client-Request-Id": "active-2"},
        json=_build_request().model_dump(mode="json"),
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "A generation job is already queued or running for this account."


def test_admin_generate_from_latest_intake_is_idempotent_for_same_client_request_id():
    client, store, _ = _build_client()
    store.create_intake("athlete-1", _build_request())

    first = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token", "X-Client-Request-Id": "admin-retry-1"},
    )
    assert first.status_code == 202

    retry_same = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token", "X-Client-Request-Id": "admin-retry-1"},
    )
    assert retry_same.status_code == 202
    assert retry_same.json()["job_id"] == first.json()["job_id"]


def test_admin_generate_from_latest_intake_requires_existing_intake():
    client, _, _ = _build_client()
    me_response = client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    assert me_response.status_code == 200

    response = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "latest intake not found for athlete"


def test_self_serve_generation_rejects_focus_picks_above_cap():
    client, _, _ = _build_client()
    request = _build_request(
        {
            "fight_date": "2099-08-20",
            "key_goals": ["power", "conditioning", "fight_sharpness", "volume"],
            "weak_areas": ["defense", "gas_tank", "timing", "footwork"],
        }
    )

    response = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer athlete-token"},
        json=request.model_dump(mode="json"),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "This camp allows 6 total focus picks. Remove 2 goal or weak-area selections before generating."


def test_admin_generation_from_latest_intake_rejects_focus_picks_above_cap():
    client, store, _ = _build_client()
    me_response = client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    assert me_response.status_code == 200
    store.create_intake(
        "athlete-1",
        _build_request(
            {
                "fight_date": "2099-08-20",
                "key_goals": ["power", "conditioning", "fight_sharpness", "volume"],
                "weak_areas": ["defense", "gas_tank", "timing", "footwork"],
            }
        ),
    )

    response = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "This camp allows 6 total focus picks. Remove 2 goal or weak-area selections before generating."


def test_admin_generation_from_latest_intake_rejects_invalid_saved_payload():
    client, store, _ = _build_client()
    me_response = client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    assert me_response.status_code == 200

    invalid_payload = _build_request().model_dump(mode="json")
    invalid_payload["athlete"] = "not-an-object"
    store.intakes.setdefault("athlete-1", []).append(
        {
            "id": "intake_invalid_payload",
            "athlete_id": "athlete-1",
            "fight_date": invalid_payload["fight_date"],
            "technical_style": [],
            "intake": invalid_payload,
            "created_at": "2026-04-01T00:00:00+00:00",
        }
    )

    response = client.post(
        "/api/admin/athletes/athlete-1/plans/generate-from-latest-intake",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "latest intake is invalid and cannot be used for generation"


def test_auth_is_required_for_draft_save():
    client, _, _ = _build_client()

    response = client.put(
        "/api/me",
        json=ProfileUpdateRequest(
            full_name="Ari Mensah",
            onboarding_draft={"current_step": 5, "injuries": "left shoulder"},
        ).model_dump(mode="json"),
    )

    assert response.status_code == 401


def test_review_stage_draft_save_persists_step_and_form():
    client, store, _ = _build_client()
    request = _build_request()
    draft_payload = {
        **request.model_dump(mode="json"),
        "current_step": 5,
    }

    response = client.put(
        "/api/me",
        headers={"Authorization": "Bearer athlete-token"},
        json=ProfileUpdateRequest(
            full_name=request.athlete.full_name,
            technical_style=request.athlete.technical_style,
            record=request.athlete.record,
            onboarding_draft=draft_payload,
        ).model_dump(mode="json"),
    )

    assert response.status_code == 200
    profile = response.json()["profile"]
    assert profile["onboarding_draft"]["current_step"] == 5
    assert profile["onboarding_draft"]["fight_date"] == request.fight_date
    assert store.profiles["athlete-1"]["onboarding_draft"]["current_step"] == 5


def test_review_stage_invalid_record_returns_422_not_network_error():
    client, _, _ = _build_client()

    for bad_record in ("5-", "-1", "5", "5-1-2-3", "abc"):
        response = client.put(
            "/api/me",
            headers={"Authorization": "Bearer athlete-token"},
            json={"record": bad_record},
        )
        assert response.status_code == 422, f"expected 422 for record={bad_record!r}"


def test_review_stage_empty_record_is_accepted_during_draft_save():
    client, _, _ = _build_client()

    response = client.put(
        "/api/me",
        headers={"Authorization": "Bearer athlete-token"},
        json=ProfileUpdateRequest(
            full_name="Ari Mensah",
            record="",
            onboarding_draft={"current_step": 5},
        ).model_dump(mode="json"),
    )

    assert response.status_code == 200


def test_saved_onboarding_draft_round_trips_through_me_and_clears_after_generation():
    client, store, _ = _build_client()

    draft_response = client.put(
        "/api/me",
        headers={"Authorization": "Bearer athlete-token"},
        json=ProfileUpdateRequest(
            full_name="Ari Mensah",
            technical_style=["boxing"],
            onboarding_draft={"current_step": 4, "injuries": "heel soreness"},
        ).model_dump(mode="json"),
    )

    assert draft_response.status_code == 200
    assert draft_response.json()["profile"]["onboarding_draft"]["current_step"] == 4

    me_response = client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    assert me_response.status_code == 200
    assert me_response.json()["profile"]["onboarding_draft"]["injuries"] == "heel soreness"

    generate_response = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer athlete-token"},
        json=_build_request().model_dump(mode="json"),
    )

    assert generate_response.status_code == 202
    assert store.profiles["athlete-1"]["onboarding_draft"] is None
    refreshed_me = client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    assert refreshed_me.json()["profile"]["onboarding_draft"] is None
    assert refreshed_me.json()["latest_intake"]["fight_date"] == "2099-04-18"


def test_onboarding_draft_endpoint_requires_auth():
    client, _, _ = _build_client()

    response = client.patch(
        "/api/onboarding/draft",
        json={"onboarding_draft": {"current_step": 3}},
    )

    assert response.status_code == 401


def test_onboarding_draft_endpoint_persists_draft_and_profile_fields():
    client, store, _ = _build_client()

    response = client.patch(
        "/api/onboarding/draft",
        headers={"Authorization": "Bearer athlete-token"},
        json={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": ["pressure_fighter"],
            "stance": "orthodox",
            "professional_status": "amateur",
            "record": "5-1",
            "athlete_timezone": "Europe/London",
            "onboarding_draft": {"current_step": 3, "injuries": "ankle soreness"},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert isinstance(payload["updated_at"], str)
    assert set(payload.keys()) == {"ok", "updated_at"}
    assert store.profiles["athlete-1"]["onboarding_draft"]["current_step"] == 3
    assert store.profiles["athlete-1"]["record_summary"] == "5-1"


def test_onboarding_draft_endpoint_omitted_fields_are_not_cleared():
    client, store, _ = _build_client()

    me_response = client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    assert me_response.status_code == 200
    original_profile = store.profiles["athlete-1"].copy()

    response = client.patch(
        "/api/onboarding/draft",
        headers={"Authorization": "Bearer athlete-token"},
        json={"onboarding_draft": {"current_step": 4}},
    )

    assert response.status_code == 200
    assert store.profiles["athlete-1"]["onboarding_draft"]["current_step"] == 4
    assert store.profiles["athlete-1"]["full_name"] == original_profile["full_name"]
    assert store.profiles["athlete-1"]["technical_style"] == original_profile["technical_style"]
    assert store.profiles["athlete-1"]["stance"] == original_profile["stance"]
    assert store.profiles["athlete-1"]["professional_status"] == original_profile["professional_status"]
    assert store.profiles["athlete-1"]["record_summary"] == original_profile["record_summary"]

def test_me_route_defaults_profile_appearance_mode_to_dark():
    client, _, _ = _build_client()

    response = client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})

    assert response.status_code == 200
    assert response.json()["profile"]["appearance_mode"] == "dark"


def test_update_me_persists_profile_appearance_mode():
    client, store, _ = _build_client()

    response = client.put(
        "/api/me",
        headers={"Authorization": "Bearer athlete-token"},
        json=ProfileUpdateRequest(
            full_name="Ari Mensah",
            appearance_mode="light",
        ).model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["profile"]["appearance_mode"] == "light"
    assert store.profiles["athlete-1"]["appearance_mode"] == "light"


def test_change_username_normalizes_to_lowercase_and_returns_rate_limit():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})

    response = client.post(
        "/api/me/username",
        headers={"Authorization": "Bearer athlete-token"},
        json={"username": "Ari.Fight"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["profile"]["username"] == "ari.fight"
    assert store.profiles["athlete-1"]["username"] == "ari.fight"
    assert body["username_rate_limit"]["max_changes_per_window"] == 4
    assert body["username_rate_limit"]["window_days"] == 30
    assert body["username_rate_limit"]["remaining"] == 3


def test_change_username_rejects_invalid_username():
    client, _, _ = _build_client()

    response = client.post(
        "/api/me/username",
        headers={"Authorization": "Bearer athlete-token"},
        json={"username": "bad username!"},
    )

    assert response.status_code == 422


def test_change_username_rejects_duplicate_username():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    other_profile = store.ensure_profile(
        AuthenticatedUser(
            user_id="athlete-2",
            email="bo@example.com",
            full_name="Bo Tran",
            metadata={},
        )
    )
    other_profile["username"] = "taken_name"

    response = client.post(
        "/api/me/username",
        headers={"Authorization": "Bearer athlete-token"},
        json={"username": "Taken_Name"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "That username is already taken. Pick another."


def test_change_username_same_username_is_noop():
    client, store, _ = _build_client()
    client.post(
        "/api/me/username",
        headers={"Authorization": "Bearer athlete-token"},
        json={"username": "same_name"},
    )
    original_history = list(store.profiles["athlete-1"]["username_change_history"])

    response = client.post(
        "/api/me/username",
        headers={"Authorization": "Bearer athlete-token"},
        json={"username": "Same_Name"},
    )

    assert response.status_code == 200
    assert response.json()["profile"]["username"] == "same_name"
    assert store.profiles["athlete-1"]["username_change_history"] == original_history


def test_change_username_rate_limits_after_four_changes_in_window():
    client, store, _ = _build_client()
    client.get("/api/me", headers={"Authorization": "Bearer athlete-token"})
    now = datetime.now(timezone.utc)
    store.profiles["athlete-1"]["username_change_history"] = [
        (now - timedelta(days=offset)).isoformat()
        for offset in (1, 2, 3, 4)
    ]

    response = client.post(
        "/api/me/username",
        headers={"Authorization": "Bearer athlete-token"},
        json={"username": "fifth_name"},
    )

    assert response.status_code == 429
    assert "You can change your username up to 4 times every 30 days." in response.json()["detail"]


def test_profile_update_rejects_overlong_profile_fields():
    client, _, _ = _build_client()

    response = client.put(
        "/api/me",
        headers={"Authorization": "Bearer athlete-token"},
        json={"full_name": "A" * 121},
    )

    assert response.status_code == 422
    assert "full_name" in str(response.json()["detail"])


def test_profile_update_normalizes_whitespace_optional_text():
    client, store, _ = _build_client()

    response = client.put(
        "/api/me",
        headers={"Authorization": "Bearer athlete-token"},
        json={"stance": "   ", "record": " 5-1 "},
    )

    assert response.status_code == 200
    assert store.profiles["athlete-1"]["stance"] == ""
    assert store.profiles["athlete-1"]["record_summary"] == "5-1"


def test_onboarding_draft_endpoint_rejects_oversized_draft_fields():
    client, _, _ = _build_client()

    response = client.patch(
        "/api/onboarding/draft",
        headers={"Authorization": "Bearer athlete-token"},
        json={"onboarding_draft": {"athlete": {"full_name": "A" * 121}}},
    )

    assert response.status_code == 422
    assert "full_name" in str(response.json()["detail"])


def test_onboarding_draft_endpoint_rejects_oversized_list_item():
    client, _, _ = _build_client()

    response = client.patch(
        "/api/onboarding/draft",
        headers={"Authorization": "Bearer athlete-token"},
        json={"onboarding_draft": {"key_goals": ["x" * 121]}},
    )

    assert response.status_code == 422
    assert "key_goals" in str(response.json()["detail"])


def test_onboarding_draft_endpoint_accepts_valid_guided_injury():
    client, store, _ = _build_client()

    response = client.patch(
        "/api/onboarding/draft",
        headers={"Authorization": "Bearer athlete-token"},
        json={
            "onboarding_draft": {
                "current_step": 3,
                "guided_injury": {
                    "area": "left shoulder",
                    "severity": "moderate",
                    "trend": "improving",
                    "avoid": "heavy overhead pressing",
                    "notes": "Irritated after sparring but settling.",
                },
            }
        },
    )

    assert response.status_code == 200
    assert store.profiles["athlete-1"]["onboarding_draft"]["guided_injury"]["area"] == "left shoulder"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("notes", "x" * 4001),
        ("avoid", "x" * 2001),
        ("area", "x" * 201),
    ],
)
def test_onboarding_draft_endpoint_rejects_oversized_guided_injury_fields(field: str, value: str):
    client, _, _ = _build_client()

    response = client.patch(
        "/api/onboarding/draft",
        headers={"Authorization": "Bearer athlete-token"},
        json={"onboarding_draft": {"guided_injury": {field: value}}},
    )

    assert response.status_code == 422
    assert "guided_injury" in str(response.json()["detail"])
    assert field in str(response.json()["detail"])


def test_onboarding_draft_endpoint_rejects_oversized_guided_injuries_list():
    client, _, _ = _build_client()

    response = client.patch(
        "/api/onboarding/draft",
        headers={"Authorization": "Bearer athlete-token"},
        json={"onboarding_draft": {"guided_injuries": [{"area": "knee"}] * 65}},
    )

    assert response.status_code == 422
    assert "guided_injuries" in str(response.json()["detail"])
    assert "64" in str(response.json()["detail"])


def test_onboarding_draft_endpoint_rejects_oversized_item_inside_guided_injuries():
    client, _, _ = _build_client()

    response = client.patch(
        "/api/onboarding/draft",
        headers={"Authorization": "Bearer athlete-token"},
        json={
            "onboarding_draft": {
                "guided_injuries": [
                    {"area": "left knee"},
                    {"area": "right ankle", "injury_subtypes": ["x" * 65]},
                ]
            }
        },
    )

    assert response.status_code == 422
    assert "guided_injuries[1]" in str(response.json()["detail"])
    assert "injury_subtypes" in str(response.json()["detail"])
