"""F1 regressions carried forward from unmerged audit #2920, with real writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from api.contracts.external_training import ExternalTrainingCreate
from api.contracts.readiness_message import ReadinessCheckin, ReadinessContext, build_readiness_adjustment
from api.services.sparring_readiness_snapshot import annotate_payload_with_sparring_readiness
from api.services.today_readiness_boundary import build_today_command_view, upsert_session_completion
from api.services.today_service import submit_today_checkin
from api.services.training_history import create_external_training, training_history
from api.services.week_progress import evaluate_week_completion
from fightcamp.completed_workload import completed_workload
from fightcamp.sparring_readiness import sparring_readiness_flags
from tests.support import FakeStore

PLAN = "11111111-1111-1111-1111-111111111111"
DAY = "2026-10-18"
NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
ATHLETE = "athlete-1"


def make_store(*, sibling="Breathing reset", optional=False):
    store = FakeStore()
    store.plans[PLAN] = {
        "id": PLAN, "athlete_id": ATHLETE, "status": "ready",
        "created_at": "2026-10-01T12:00:00Z",
        "structured_plan": {"weeks": [{"week_id": "week-1", "week_index": 1,
            "start_date": DAY, "end_date": "2026-10-24", "phase_label": "SPP", "days": [{
                "date": DAY, "day_type": "moderate", "sessions": [
                    {"session_id": "main", "session_type": "conditioning", "title": "Conditioning", "blocks": []},
                    {"session_id": "sibling", "session_type": "sparring" if "sparring" in sibling.lower() else "strength" if sibling == "Strength" else "skill" if sibling == "Boxing" else "recovery",
                     "title": sibling, "optional": optional, "blocks": []},
                ],
            }]}]},
    }
    store.set_active_plan_id(ATHLETE, PLAN)
    # Logging today's session needs today's check-in (session_hold). Tests that
    # plant their own check-in for the day replace this one.
    submit_today_checkin(store, athlete_id=ATHLETE, athlete_timezone="Europe/London", now=NOW, payload={
        "plan_id": PLAN, "sleep": "good", "body": "normal", "pain": "none", "phase": "SPP",
        "active_injury": "none", "previous_session": "none", "sharp_pain": False, "instability": False,
        "swelling": False, "neurological_symptoms": False, "illness_symptoms": False,
        "cannot_warm_into_movement": False, "worse_next_day_pain": False})
    return store


def report(store, session="main", status="done", now=NOW, **changes):
    return upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="Europe/London", now=now,
        payload={"plan_id": PLAN, "session_id": session, "status": status, "session_rpe": 8,
                 "pain_after": 2, "notes": "First workout", "modification_reason": "Adjusted" if status in {"modified", "skipped"} else "", **changes})


def snapshot(store):
    return annotate_payload_with_sparring_readiness({}, store=store, athlete_id=ATHLETE,
        training_day=DAY)["_sparring_readiness"]


@pytest.mark.parametrize("status", ["done", "modified"])
def test_one_execution_plus_support_is_one_workout_and_preserves_day_progress(status):
    store = make_store()
    first = report(store, status=status)
    sibling = store.get_session_completion(ATHLETE, "sibling", DAY)
    assert first["completion_origin"] == "reported"
    assert sibling["completion_origin"] == "day_credit" and sibling["status"] == status
    assert sibling["session_rpe"] is sibling["pain_after"] is None
    assert sibling["notes"] == "" and not sibling.get("prescription_snapshot")
    rows = training_history(store, ATHLETE)
    assert [row["id"] for row in rows] == [first["id"]]
    assert completed_workload(rows, DAY)["demanding_sessions"] == 1
    context = ReadinessContext(training_day=DAY, phase="SPP", recent_sessions=rows,
        today_session={"session_type": "conditioning", "title": "Conditioning"})
    assert "recent_hard_load_plus_poor_today" not in build_readiness_adjustment(ReadinessCheckin(body="flat"), context).triggers
    plan = store.plans[PLAN]
    evaluation = evaluate_week_completion(completions=store.list_plan_session_completions(ATHLETE, PLAN),
                                         week=plan["structured_plan"]["weeks"][0])
    assert evaluation["complete"] is True
    assert "high_fatigue" not in sparring_readiness_flags(snapshot(store))


@pytest.mark.parametrize("second_status", ["done", "modified", "started", "skipped", "not_started"])
def test_later_independent_report_does_not_overwrite_first_actuals(second_status):
    store = make_store(sibling="Boxing")
    first = report(store, session_rpe=6)
    before = store.get_session_completion(ATHLETE, "main", DAY)
    second = report(store, "sibling", second_status, now=NOW + timedelta(hours=3),
                    session_rpe=9, pain_after=5, notes="Second workout")
    assert store.get_session_completion(ATHLETE, "main", DAY) == before
    assert second["id"] != first["id"]
    if second_status in {"done", "modified"}:
        assert second["_entered_completed_status"] is True
        assert second["completed_at"] != first["completed_at"]
        rows = training_history(store, ATHLETE)
        assert len(rows) == 2
        assert {row["session_rpe"] for row in rows} == {6, 9}
    else:
        assert completed_workload(training_history(store, ATHLETE), DAY)["sessions"] == 1


def test_two_confirmed_same_day_app_workouts_trigger_existing_rule_and_retry_is_stable():
    store = make_store(sibling="Strength")
    first = report(store)
    second = report(store, "sibling", now=NOW + timedelta(hours=3))
    repeated = report(store, "sibling", now=NOW + timedelta(hours=4))
    assert repeated["id"] == second["id"] and repeated["completed_at"] == second["completed_at"]
    rows = training_history(store, ATHLETE)
    assert {row["id"] for row in rows} == {first["id"], second["id"]}
    assert completed_workload(rows + rows, DAY)["demanding_sessions"] == 2
    context = ReadinessContext(training_day=DAY, phase="SPP", recent_sessions=rows)
    assert "recent_hard_load_plus_poor_today" in build_readiness_adjustment(ReadinessCheckin(body="flat"), context).triggers


def test_strength_and_independent_manual_boxing_remain_two_workouts():
    from uuid import UUID
    store = make_store()
    main = store.plans[PLAN]["structured_plan"]["weeks"][0]["days"][0]["sessions"][0]
    main.update(session_type="strength", title="Strength")
    report(store)
    create_external_training(store, ATHLETE, "Europe/London", ExternalTrainingCreate(
        activity="boxing", duration_minutes=60, effort="hard",
        client_request_id=UUID("22222222-2222-4222-8222-222222222222")), now=NOW)
    rows = training_history(store, ATHLETE)
    assert {row["source"] for row in rows} == {"external", "unlxck"}
    assert completed_workload(rows, DAY)["sessions"] == 2


@pytest.mark.parametrize("status", ["started", "skipped", "not_started"])
def test_uncompleted_day_credit_never_becomes_workload(status):
    store = make_store()
    report(store, status=status)
    assert store.get_session_completion(ATHLETE, "sibling", DAY)["completion_origin"] == "day_credit"
    assert completed_workload(store.list_session_completions(ATHLETE), DAY)["sessions"] == 0
    assert not sparring_readiness_flags(snapshot(store))


@pytest.mark.parametrize("status", ["started", "skipped"])
def test_uncompleted_and_optional_components_never_inflate_workload(status):
    store = make_store(optional=True)
    report(store, status=status)
    assert store.get_session_completion(ATHLETE, "sibling", DAY) is None
    assert completed_workload(training_history(store, ATHLETE), DAY)["sessions"] == 0
    if status == "skipped":
        assert len(training_history(store, ATHLETE, include_skipped=True)) == 1


def test_unperformed_sparring_sibling_is_not_contact_or_rehab_evidence():
    store = make_store(sibling="Hard sparring")
    report(store)
    contact = snapshot(store)
    assert [row["session_id"] for row in contact["sessions"]] == ["main"]
    assert contact["sessions"][0]["contact_load"] == "none"
    assert not sparring_readiness_flags(contact)
    credit = store.get_session_completion(ATHLETE, "sibling", DAY)
    assert not credit.get("rehab_response_contexts") and not store.rehab_exposures
    report(store, "sibling")
    assert next(row for row in snapshot(store)["sessions"] if row["session_id"] == "sibling")["contact_load"] == "hard"


def test_legacy_rows_remain_unmodified_unverified_and_fail_conservatively():
    store = make_store(sibling="Hard sparring")
    for session in ["main", "sibling"]:
        store.upsert_session_completion(ATHLETE, {"plan_id": PLAN, "session_id": session,
            "training_day": DAY, "status": "done", "session_rpe": 9, "pain_after": 6, "notes": "Legacy"})
    original = deepcopy(store.session_completions)
    rows = training_history(store, ATHLETE)
    assert len(rows) == 2 and all(row["completion_origin"] is None for row in rows)
    assert completed_workload(rows, DAY)["sessions"] == 0
    contact = snapshot(store)
    assert contact["sessions"] == []
    assert contact["training_history"]["available"] is False
    assert "sparring_history_unavailable" in sparring_readiness_flags(contact)
    store.upsert_today_checkin(ATHLETE, {"plan_id": PLAN, "training_day": DAY,
        "sleep": "good", "body": "sharp", "pain": "none", "recommendation_state": "train_as_planned"})
    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="Europe/London", now=NOW)
    assert view.today.recommendation_state != "train_as_planned"
    assert store.session_completions == original
    report(store)
    assert store.get_session_completion(ATHLETE, "sibling", DAY) == original[ATHLETE][1]


@pytest.mark.parametrize("zone,now,day", [
    ("America/New_York", datetime(2026, 10, 19, 2, tzinfo=timezone.utc), DAY),
    ("Europe/London", NOW + timedelta(days=1), DAY),
])
def test_local_day_and_explicit_backdate_preserve_occurrence(zone, now, day):
    store = make_store()
    payload = {"plan_id": PLAN, "session_id": "main", "status": "done", "session_rpe": 8, "completion_origin": "day_credit"}
    if zone == "Europe/London":
        payload["training_day"] = day
    first = upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone=zone, now=now, payload=payload)
    assert first["training_day"] == day
    assert completed_workload(training_history(store, ATHLETE), day)["sessions"] == 1


@pytest.mark.parametrize("restriction", ["neurological", "severe_injury"])
def test_explicit_report_replaces_day_credit_under_advisory_warnings(restriction):
    store = make_store()
    report(store)
    if restriction == "neurological":
        store.upsert_today_checkin(ATHLETE, {"plan_id": PLAN, "training_day": DAY,
            "sleep": "good", "body": "sharp", "pain": "none", "neurological_symptoms": True,
            "recommendation_state": "pull_back", "recommendation_triggers": ["neurological_symptoms"]})
    else:
        store.create_injury_flag(ATHLETE, {
            "body_area": "knee", "description": "Knee injury", "severity": "severe", "status": "open"})
    row = report(store, "sibling")
    assert row["status"] == "done"
    assert store.get_session_completion(ATHLETE, "sibling", DAY)["completion_origin"] == "reported"


def test_route_preserves_daily_xp_streaks_week_progress_and_history(monkeypatch):
    from api.services import today_readiness_boundary, today_readiness_boundary_core, today_service
    from tests.support import _build_client
    # All imported resolver functions share this deterministic clock.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(today_service, "datetime", Clock)
    monkeypatch.setattr(today_readiness_boundary, "datetime", Clock)
    monkeypatch.setattr(today_readiness_boundary_core, "datetime", Clock)
    client, store, _ = _build_client()
    store.plans = make_store().plans
    store.set_active_plan_id(ATHLETE, PLAN)
    headers = {"Authorization": "Bearer athlete-token"}
    payload = {"plan_id": PLAN, "session_id": "main", "status": "done", "session_rpe": 8}
    # Logging today's session needs today's check-in (session_hold).
    checkin = client.post("/api/today/checkin", headers=headers, json={
        "plan_id": PLAN, "sleep": "good", "body": "normal", "pain": "none", "phase": "SPP"})
    assert checkin.status_code == 201, checkin.text
    for _ in range(2):
        response = client.post("/api/today/session-completion", headers=headers, json=payload)
        assert response.status_code == 201, response.text
    rows = client.get("/api/training/history", headers=headers).json()["sessions"]
    assert [row["completion_origin"] for row in rows] == ["reported"]
    assert len(store.session_completions[ATHLETE]) == 2
    # The required check-in earns its own XP; logging the session twice must
    # still award each session action once.
    session_awards = [row for row in store.xp_awards[ATHLETE]
                      if row["action"] not in {"readiness_checkin_completed", "first_checkin_completed"}]
    assert {row["action"] for row in session_awards} == {
        "training_logged", "planned_session_completed", "full_training_week_completed"}
    assert len(session_awards) == 3
    assert store.get_athlete_streaks(ATHLETE)["training_current"] == 1
    assert store.get_athlete_streaks(ATHLETE)["adherence_current"] == 1
