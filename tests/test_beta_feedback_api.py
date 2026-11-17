from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from PIL import Image

from api.services import feedback_service
from api.services.today_service import resolve_training_day
from api.services.feedback_service import report_limit_per_hour, screenshot_limit_per_hour
from api.routes import feedback as feedback_routes
from tests.support import _build_client

ATHLETE = {"Authorization": "Bearer athlete-token"}
ADMIN = {"Authorization": "Bearer admin-token"}
PLAN_ID = "11111111-1111-1111-1111-111111111111"
OTHER_PLAN_ID = "22222222-2222-2222-2222-222222222222"


def _seed_plan(
    store,
    *,
    plan_id: str = PLAN_ID,
    athlete_id: str = "athlete-1",
    **overrides,
) -> None:
    store.plans[plan_id] = {
        "id": plan_id,
        "athlete_id": athlete_id,
        "intake_id": None,
        "status": "ready",
        "plan_text": "Released plan",
        "created_at": datetime.now(timezone.utc).isoformat(),
        **overrides,
    }


def _seed_today(store, *, athlete_id: str = "athlete-1") -> None:
    _seed_plan(store, athlete_id=athlete_id)
    store.active_plan_ids[athlete_id] = PLAN_ID
    store.today_checkins[athlete_id] = [
        {
            "id": "33333333-3333-3333-3333-333333333333",
            "athlete_id": athlete_id,
            "plan_id": PLAN_ID,
            "training_day": resolve_training_day(""),
            "phase": "GPP",
            "sleep": "good",
            "body": "normal",
            "pain": "none",
            "recommendation_state": "train_as_planned",
            "recommendation_reason": "",
            "recommendation_triggers": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ]


def test_plan_feedback_is_owned_and_idempotent():
    client, store, _ = _build_client()
    _seed_plan(store)
    first = client.put(
        f"/api/plans/{PLAN_ID}/feedback",
        headers=ATHLETE,
        json={"response": "no", "reason": "too_hard", "comment": "Reduce volume"},
    )
    assert first.status_code == 200
    saved = store.beta_feedback[0]
    assert saved["submitted_by_profile_id"] == "athlete-1"
    assert saved["context_key"] == f"plan:{PLAN_ID}"
    assert saved["surface"] == "plan"
    assert saved["category"] == "plan_usefulness"
    assert saved["priority"] == "normal"

    second = client.put(
        f"/api/plans/{PLAN_ID}/feedback",
        headers=ATHLETE,
        json={"response": "yes", "comment": "stale client complaint"},
    )
    assert second.status_code == 200
    assert len(store.beta_feedback) == 1
    assert store.beta_feedback[0]["response"] == "yes"
    assert store.beta_feedback[0]["reason"] is None
    assert store.beta_feedback[0]["comment"] == ""

    assert client.get(f"/api/plans/{PLAN_ID}/feedback", headers=ADMIN).status_code == 404
    _seed_plan(store, plan_id=OTHER_PLAN_ID, athlete_id="other-athlete")
    assert client.get(f"/api/plans/{OTHER_PLAN_ID}/feedback", headers=ATHLETE).status_code == 404


@pytest.mark.parametrize("role", ["coach", "gym_owner"])
def test_non_training_roles_are_rejected_from_contextual_feedback(role: str):
    client, store, _ = _build_client()
    _seed_plan(store)
    store.profiles["athlete-1"]["role"] = role
    response = client.get(f"/api/plans/{PLAN_ID}/feedback", headers=ATHLETE)
    assert response.status_code == 403


def test_admin_can_submit_contextual_feedback_for_their_own_plan():
    client, store, _ = _build_client()
    _seed_plan(store, athlete_id="admin-1")

    response = client.put(
        f"/api/plans/{PLAN_ID}/feedback",
        headers=ADMIN,
        json={"response": "yes"},
    )

    assert response.status_code == 200
    assert store.beta_feedback[-1]["submitted_by_profile_id"] == "admin-1"
    assert store.beta_feedback[-1]["plan_id"] == PLAN_ID


def test_admin_today_feedback_is_saved_and_queued_for_operator_delivery(monkeypatch):
    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        feedback_routes,
        "send_feedback_notification",
        lambda record, profile: delivered.append((record.id, profile.profile_id)),
    )
    client, store, _ = _build_client()
    _seed_today(store, athlete_id="admin-1")

    response = client.put(
        "/api/today/feedback",
        headers=ADMIN,
        json={"response": "yes"},
    )

    assert response.status_code == 200
    assert store.beta_feedback[-1]["submitted_by_profile_id"] == "admin-1"
    assert store.beta_feedback[-1]["today_checkin_id"] == "33333333-3333-3333-3333-333333333333"
    assert delivered == [(response.json()["id"], "admin-1")]


def test_contextual_enums_and_forbidden_client_context_are_rejected():
    client, store, _ = _build_client()
    _seed_plan(store)
    invalid_reason = client.put(
        f"/api/plans/{PLAN_ID}/feedback",
        headers=ATHLETE,
        json={"response": "no", "reason": "too_demanding"},
    )
    assert invalid_reason.status_code == 422
    forbidden = client.put(
        f"/api/plans/{PLAN_ID}/feedback",
        headers=ATHLETE,
        json={"response": "yes", "athlete_id": "victim", "priority": "safety", "surface": "global"},
    )
    assert forbidden.status_code == 422
    assert not store.beta_feedback


@pytest.mark.parametrize(
    "plan_overrides",
    [
        {"status": "generated", "plan_text": "Draft plan"},
        {"status": "ready", "plan_text": ""},
    ],
)
def test_plan_feedback_rejects_unreleased_or_incomplete_plans(plan_overrides: dict):
    client, store, _ = _build_client()
    _seed_plan(store, **plan_overrides)
    get_response = client.get(f"/api/plans/{PLAN_ID}/feedback", headers=ATHLETE)
    put_response = client.put(
        f"/api/plans/{PLAN_ID}/feedback",
        headers=ATHLETE,
        json={"response": "yes"},
    )
    assert get_response.status_code == 409
    assert put_response.status_code == 409
    assert not store.beta_feedback


def test_plan_feedback_snapshots_real_nested_intake_shape():
    client, store, _ = _build_client()
    intake_id = "intake-nested"
    _seed_plan(store, intake_id=intake_id)
    store.intakes["athlete-1"] = [
        {
            "id": intake_id,
            "athlete_id": "athlete-1",
            "injuries": "wrong flattened value",
            "intake": {
                "fatigue_level": "high",
                "injuries": "left shoulder restriction",
                "guided_injury": {"body_area": "shoulder", "severity": "moderate"},
                "guided_injuries": [{"body_area": "knee", "severity": "low"}],
                "training_restriction_level": "moderate",
                "training_availability": ["Monday", "Thursday"],
                "phase_override": "SPP",
            },
        }
    ]
    response = client.put(
        f"/api/plans/{PLAN_ID}/feedback",
        headers=ATHLETE,
        json={"response": "no", "reason": "injury_restrictions_wrong"},
    )
    assert response.status_code == 200
    snapshot = store.beta_feedback[-1]["injury_snapshot"]["intake"]
    assert snapshot == {
        "fatigue_level": "high",
        "injuries": "left shoulder restriction",
        "guided_injury": {"body_area": "shoulder", "severity": "moderate"},
        "guided_injuries": [{"body_area": "knee", "severity": "low"}],
        "training_restriction_level": "moderate",
        "training_availability": ["Monday", "Thursday"],
        "phase_override": "SPP",
    }


def test_plan_feedback_still_saves_when_optional_health_context_is_unavailable():
    client, store, _ = _build_client()
    _seed_plan(store, intake_id="missing-context")

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("optional context unavailable")

    store.list_feedback_injury_flags = unavailable
    store.get_feedback_intake = unavailable

    response = client.put(
        f"/api/plans/{PLAN_ID}/feedback",
        headers=ATHLETE,
        json={"response": "yes"},
    )

    assert response.status_code == 200
    assert store.beta_feedback[-1]["injury_snapshot"] == {"open_flags": [], "intake": {}}


def test_plan_feedback_still_saves_when_legacy_phase_context_cannot_be_derived(monkeypatch):
    client, store, _ = _build_client()
    _seed_plan(store)

    def unavailable(*_args, **_kwargs):
        raise ValueError("legacy phase unavailable")

    monkeypatch.setattr(feedback_service, "resolve_current_week", unavailable)

    response = client.put(
        f"/api/plans/{PLAN_ID}/feedback",
        headers=ATHLETE,
        json={"response": "yes"},
    )

    assert response.status_code == 200
    assert store.beta_feedback[-1]["camp_phase"] is None


def test_today_unsafe_feedback_is_server_derived_and_does_not_mutate_programme():
    client, store, _ = _build_client()
    _seed_today(store)
    original_plan = dict(store.plans[PLAN_ID])
    original_checkin = dict(store.today_checkins["athlete-1"][0])
    response = client.put(
        "/api/today/feedback",
        headers=ATHLETE,
        json={"response": "unsafe", "comment": "Pain increased"},
    )
    assert response.status_code == 200
    row = store.beta_feedback[0]
    assert row["category"] == "recommendation_safety"
    assert row["priority"] == "safety"
    assert row["today_checkin_id"] == original_checkin["id"]
    assert row["readiness_snapshot"]["pain"] == "none"
    assert store.plans[PLAN_ID] == original_plan
    assert store.today_checkins["athlete-1"][0] == original_checkin


def test_today_feedback_still_saves_when_optional_injury_context_is_unavailable():
    client, store, _ = _build_client()
    _seed_today(store)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("optional context unavailable")

    store.list_feedback_injury_flags = unavailable

    response = client.put(
        "/api/today/feedback",
        headers=ATHLETE,
        json={"response": "yes"},
    )

    assert response.status_code == 200
    assert store.beta_feedback[-1]["injury_snapshot"] == {"open_flags": []}


def test_today_feedback_rejects_checkin_without_generated_recommendation():
    client, store, _ = _build_client()
    _seed_today(store)
    store.today_checkins["athlete-1"][0]["recommendation_state"] = ""
    get_response = client.get("/api/today/feedback", headers=ATHLETE)
    put_response = client.put(
        "/api/today/feedback",
        headers=ATHLETE,
        json={"response": "yes"},
    )
    assert get_response.status_code == 409
    assert put_response.status_code == 409
    assert not store.beta_feedback


@pytest.mark.parametrize(
    "plan_overrides",
    [
        {"status": "generated", "plan_text": "Draft plan"},
        {"status": "archived", "plan_text": "Released historical plan"},
        {"status": "ready", "plan_text": ""},
    ],
)
def test_today_feedback_accepts_the_recommendation_already_shown_even_if_plan_is_not_feedback_eligible(
    plan_overrides: dict,
):
    client, store, _ = _build_client()
    _seed_today(store)
    store.plans[PLAN_ID].update(plan_overrides)

    existing = client.get("/api/today/feedback", headers=ATHLETE)
    submitted = client.put(
        "/api/today/feedback",
        headers=ATHLETE,
        json={"response": "yes"},
    )

    assert existing.status_code == 200
    assert existing.json() is None
    assert submitted.status_code == 200
    assert store.beta_feedback[-1]["today_checkin_id"] == "33333333-3333-3333-3333-333333333333"


def test_today_feedback_follows_the_resolved_active_plan_when_the_pointer_is_stale():
    client, store, _ = _build_client()
    _seed_today(store)
    # The explicit pointer references an archived plan, so the Today page renders
    # the recommendation from the latest eligible plan. Feedback must resolve the
    # plan the same way instead of trusting the raw pointer.
    _seed_plan(store, plan_id=OTHER_PLAN_ID, status="archived")
    store.active_plan_ids["athlete-1"] = OTHER_PLAN_ID

    existing = client.get("/api/today/feedback", headers=ATHLETE)
    submitted = client.put(
        "/api/today/feedback",
        headers=ATHLETE,
        json={"response": "yes"},
    )

    assert existing.status_code == 200
    assert existing.json() is None
    assert submitted.status_code == 200
    saved = store.beta_feedback[-1]
    assert saved["plan_id"] == PLAN_ID
    assert saved["today_checkin_id"] == "33333333-3333-3333-3333-333333333333"


def test_today_feedback_works_without_an_explicit_active_plan_pointer():
    client, store, _ = _build_client()
    _seed_today(store)
    store.active_plan_ids.pop("athlete-1", None)

    submitted = client.put(
        "/api/today/feedback",
        headers=ATHLETE,
        json={"response": "yes"},
    )

    assert submitted.status_code == 200
    assert store.beta_feedback[-1]["today_checkin_id"] == "33333333-3333-3333-3333-333333333333"


def test_today_feedback_falls_back_to_todays_stored_checkin_after_a_same_day_plan_switch():
    client, store, _ = _build_client()
    _seed_today(store)
    # The athlete checked in, then activated a different plan the same day: the
    # daily answers are shared, while feedback describes the current plan.
    store.plans[PLAN_ID]["status"] = "archived"
    _seed_plan(store, plan_id=OTHER_PLAN_ID)
    store.active_plan_ids["athlete-1"] = OTHER_PLAN_ID

    submitted = client.put(
        "/api/today/feedback",
        headers=ATHLETE,
        json={"response": "yes"},
    )

    assert submitted.status_code == 200
    saved = store.beta_feedback[-1]
    assert saved["plan_id"] == OTHER_PLAN_ID
    assert saved["today_checkin_id"] == "33333333-3333-3333-3333-333333333333"


@pytest.mark.parametrize(
    "reason",
    [
        "too_hard",
        "too_easy",
        "schedule_mismatch",
        "injury_restrictions_wrong",
        "exercises_unsuitable",
        "instructions_unclear",
        "other",
    ],
)
def test_every_plan_reason_code_is_accepted(reason: str):
    client, store, _ = _build_client()
    _seed_plan(store)
    response = client.put(
        f"/api/plans/{PLAN_ID}/feedback",
        headers=ATHLETE,
        json={"response": "no", "reason": reason},
    )
    assert response.status_code == 200
    assert store.beta_feedback[-1]["reason"] == reason


@pytest.mark.parametrize(
    "reason",
    [
        "too_demanding",
        "too_cautious",
        "pain_or_injury_ignored",
        "training_mismatch",
        "repetitive",
        "unclear",
    ],
)
def test_every_daily_reason_code_is_accepted(reason: str):
    client, store, _ = _build_client()
    _seed_today(store)
    response = client.put(
        "/api/today/feedback",
        headers=ATHLETE,
        json={"response": "no", "reason": reason},
    )
    assert response.status_code == 200
    assert store.beta_feedback[-1]["reason"] == reason


def test_global_feedback_accepts_athlete_and_admin_and_derives_identity():
    client, store, _ = _build_client()
    _seed_today(store)
    athlete = client.post(
        "/api/feedback/global",
        headers=ATHLETE,
        data={
            "category": "bug_report",
            "description": "Button clipped",
            "contact_allowed": "true",
            "submitted_by_profile_id": "victim",
            "screenshot_path": "forged/path",
        },
    )
    assert athlete.status_code == 201
    athlete_row = store.beta_feedback[-1]
    assert athlete_row["submitted_by_profile_id"] == "athlete-1"
    assert athlete_row["plan_id"] == PLAN_ID
    assert athlete_row["today_checkin_id"] is not None
    assert athlete_row["screenshot_path"] is None
    assert "athlete_id" not in athlete_row

    _seed_today(store, athlete_id="admin-1")
    admin = client.post(
        "/api/feedback/global",
        headers=ADMIN,
        data={"category": "feature_request", "description": "Export view"},
    )
    assert admin.status_code == 201
    admin_row = store.beta_feedback[-1]
    assert admin_row["submitted_by_profile_id"] == "admin-1"
    assert admin_row["plan_id"] == PLAN_ID
    assert admin_row["today_checkin_id"] == "33333333-3333-3333-3333-333333333333"
    assert admin_row["readiness_snapshot"]["recommendation_state"] == "train_as_planned"


def test_global_feedback_still_saves_when_optional_programme_context_is_unavailable():
    client, store, _ = _build_client()

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("optional context unavailable")

    store.get_feedback_active_plan_id = unavailable
    store.list_feedback_injury_flags = unavailable

    response = client.post(
        "/api/feedback/global",
        headers=ATHLETE,
        data={"category": "bug_report", "description": "Settings issue"},
    )

    assert response.status_code == 201
    row = store.beta_feedback[-1]
    assert row["plan_id"] is None
    assert row["today_checkin_id"] is None
    assert row["injury_snapshot"] == {"open_flags": []}


def test_admin_feedback_feed_is_admin_only_and_safety_first():
    client, store, _ = _build_client()
    _seed_plan(store)
    normal = client.put(
        f"/api/plans/{PLAN_ID}/feedback",
        headers=ATHLETE,
        json={"response": "no", "reason": "too_hard", "comment": "Reduce volume"},
    )
    safety = client.post(
        "/api/feedback/global",
        headers=ATHLETE,
        data={"category": "safety_issue", "description": "Unsafe loading", "contact_allowed": "true"},
    )
    assert normal.status_code == 200
    assert safety.status_code == 201

    assert client.get("/api/admin/feedback", headers=ATHLETE).status_code == 403
    response = client.get("/api/admin/feedback?limit=20", headers=ADMIN)
    assert response.status_code == 200
    rows = response.json()
    assert [row["priority"] for row in rows] == ["safety", "normal"]
    assert rows[0]["submitter_email"] == "ari@example.com"
    assert rows[0]["comment"] == "Unsafe loading"
    assert rows[0]["contact_allowed"] is True


def test_admin_feedback_feed_summarises_server_context_when_comment_is_empty():
    client, store, _ = _build_client()
    _seed_today(store)
    store.injury_flags["athlete-1"] = [
        {
            "id": "injury-1",
            "athlete_id": "athlete-1",
            "plan_id": PLAN_ID,
            "body_area": "left shoulder",
            "severity": "moderate",
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ]
    response = client.put(
        "/api/today/feedback",
        headers={
            **ATHLETE,
            "referer": "http://testserver/today",
            "sec-ch-ua": '"Test Browser";v="1"',
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-mobile": "?0",
            "accept-language": "en-GB",
        },
        json={"response": "yes"},
    )
    assert response.status_code == 200

    admin_response = client.get("/api/admin/feedback", headers=ADMIN)
    assert admin_response.status_code == 200
    row = next(item for item in admin_response.json() if item["id"] == response.json()["id"])
    assert row["comment"] == ""
    assert row["page_path"] == "/today"
    assert row["device_context"] == 'Desktop · Windows · "Test Browser";v="1"'
    assert row["language"] == "en-GB"
    assert "Pain: none" in row["readiness_context"]
    assert "Recommendation State: train_as_planned" in row["readiness_context"]
    assert row["injury_context"] == ["left shoulder · moderate · open"]
    assert row["readiness_snapshot"]["pain"] == "none"
    assert row["injury_snapshot"]["open_flags"][0]["body_area"] == "left shoulder"
    assert row["technical_context"]["referer_path"] == "/today"
    assert row["plan_id"] == PLAN_ID
    assert row["today_checkin_id"] == "33333333-3333-3333-3333-333333333333"


def test_admin_feedback_injury_summary_skips_malformed_flags_and_counts_valid_ones():
    valid = [
        {"body_area": f"area-{index}", "severity": "moderate", "status": "open"}
        for index in range(4)
    ]
    row = {
        "id": "feedback-1",
        "injury_snapshot": {
            "open_flags": ["not-a-dict", None, {"unrelated": "field"}, *valid],
        },
    }

    record = feedback_routes._admin_feedback_record(row)

    assert record.injury_context == [
        "area-0 · moderate · open",
        "area-1 · moderate · open",
        "area-2 · moderate · open",
        "4 open injury flags total",
    ]

    row["injury_snapshot"]["open_flags"] = ["not-a-dict", *valid[:3]]
    record = feedback_routes._admin_feedback_record(row)
    assert record.injury_context == [
        "area-0 · moderate · open",
        "area-1 · moderate · open",
        "area-2 · moderate · open",
    ]


def test_global_screenshot_is_sanitised_private_and_rate_limited(monkeypatch):
    monkeypatch.setenv("FEEDBACK_REPORT_LIMIT_PER_HOUR", "5")
    monkeypatch.setenv("FEEDBACK_SCREENSHOT_LIMIT_PER_HOUR", "1")
    client, store, _ = _build_client()
    image = Image.new("RGB", (24, 16), "red")
    raw = io.BytesIO()
    exif = Image.Exif()
    exif[0x010E] = "private"
    image.save(raw, format="JPEG", exif=exif)
    files = {"screenshot": ("private-name.jpg", raw.getvalue(), "image/jpeg")}
    first = client.post(
        "/api/feedback/global",
        headers=ATHLETE,
        data={"category": "safety_issue", "description": "Unsafe state"},
        files=files,
    )
    assert first.status_code == 201
    row = store.beta_feedback[-1]
    assert row["priority"] == "safety"
    assert row["screenshot_path"].startswith("athlete-1/")
    expires = datetime.fromisoformat(row["screenshot_expires_at"])
    assert 89 <= (expires - datetime.now(timezone.utc)).days <= 90
    stored, mime = store.feedback_screenshots[row["screenshot_path"]]
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(stored)) as clean:
        assert clean.getexif() == {}

    screenshot_route = f"/api/admin/feedback/{row['id']}/screenshot"
    assert client.get(screenshot_route, headers=ATHLETE).status_code == 403
    admin_access = client.get(screenshot_route, headers=ADMIN)
    assert admin_access.status_code == 200
    assert admin_access.json()["expires_in"] == 60
    assert admin_access.json()["url"].startswith("https://storage.test/signed/")
    row["screenshot_expires_at"] = "2026-01-01T00:00:00+00:00"
    assert client.get(screenshot_route, headers=ADMIN).status_code == 404

    second = client.post(
        "/api/feedback/global",
        headers=ATHLETE,
        data={"category": "bug_report"},
        files=files,
    )
    assert second.status_code == 429
    assert second.headers["retry-after"]
    assert second.json()["detail"]["code"] == "screenshot_rate_limited"


def test_global_feedback_service_runs_off_the_async_api_thread(monkeypatch):
    observed_threads: list[str] = []
    original = feedback_routes.submit_global_feedback

    def capture_thread(*args, **kwargs):
        observed_threads.append(threading.current_thread().name)
        return original(*args, **kwargs)

    monkeypatch.setattr(feedback_routes, "submit_global_feedback", capture_thread)
    client, _store, _ = _build_client()
    response = client.post(
        "/api/feedback/global",
        headers=ATHLETE,
        data={"category": "bug_report"},
    )
    assert response.status_code == 201
    assert observed_threads
    assert any("worker" in name.lower() for name in observed_threads)


def test_ambiguous_upload_failure_triggers_best_effort_object_cleanup():
    client, store, _ = _build_client()
    image = Image.new("RGB", (24, 16), "red")
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    def upload_then_timeout(path: str, data: bytes, mime: str) -> None:
        store.feedback_screenshots[path] = (data, mime)
        raise RuntimeError("ambiguous storage timeout")

    store.upload_feedback_screenshot = upload_then_timeout
    response = client.post(
        "/api/feedback/global",
        headers=ATHLETE,
        data={"category": "bug_report"},
        files={"screenshot": ("screen.png", raw.getvalue(), "image/png")},
    )
    assert response.status_code == 500
    assert not store.feedback_screenshots
    assert not store.beta_feedback


def test_malformed_upload_within_file_limit_claims_slots_before_decode(monkeypatch):
    monkeypatch.setenv("FEEDBACK_REPORT_LIMIT_PER_HOUR", "1")
    monkeypatch.setenv("FEEDBACK_SCREENSHOT_LIMIT_PER_HOUR", "1")
    client, _store, _ = _build_client()
    malformed = b"not-an-image" * 100_000
    first = client.post(
        "/api/feedback/global",
        headers=ATHLETE,
        data={"category": "bug_report"},
        files={"screenshot": ("bad.png", malformed, "image/png")},
    )
    assert first.status_code == 422
    second = client.post(
        "/api/feedback/global",
        headers=ATHLETE,
        data={"category": "bug_report"},
    )
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "feedback_rate_limited"


def test_trailing_slash_uses_feedback_multipart_body_limit():
    client, _store, _ = _build_client()
    response = client.post(
        "/api/feedback/global/",
        headers=ATHLETE,
        data={"category": "bug_report"},
        files={"screenshot": ("large.png", b"x" * (1024 * 1024 + 64 * 1024), "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 307


def test_report_rate_limit_can_be_disabled(monkeypatch):
    monkeypatch.setenv("FEEDBACK_REPORT_LIMIT_PER_HOUR", "0")
    monkeypatch.setenv("FEEDBACK_SCREENSHOT_LIMIT_PER_HOUR", "0")
    client, store, _ = _build_client()
    for _ in range(8):
        response = client.post(
            "/api/feedback/global",
            headers=ATHLETE,
            data={"category": "general_feedback"},
        )
        assert response.status_code == 201
    assert len(store.beta_feedback) == 8


def test_invalid_rate_limit_values_fall_back_without_logging_payload(monkeypatch, caplog):
    monkeypatch.setenv("FEEDBACK_REPORT_LIMIT_PER_HOUR", "sensitive-invalid-value")
    monkeypatch.setenv("FEEDBACK_SCREENSHOT_LIMIT_PER_HOUR", "-4")
    assert report_limit_per_hour() == 5
    assert screenshot_limit_per_hour() == 2
    assert "sensitive-invalid-value" not in caplog.text
    assert "-4" not in caplog.text


def test_concurrent_rate_limit_claims_do_not_exceed_configured_limit():
    _, store, _ = _build_client()

    def claim() -> bool:
        allowed, _, _ = store.claim_feedback_rate_limit(
            "athlete-1",
            report_limit=5,
            screenshot_limit=0,
            has_screenshot=False,
        )
        return allowed

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _: claim(), range(20)))
    assert sum(results) == 5
