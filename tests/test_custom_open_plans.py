"""Custom prescriptions use the shared Open Plan execution and history paths."""
import copy
import json
from datetime import date, timedelta
from uuid import uuid4

import pytest

from api.contracts.injury_policy import reconcile_session_prescription
from api.models import PlanRequest
from api.plan_mappers import _map_plan_detail, _map_profile_row
from api.services.active_plan import set_active_plan
from api.services.custom_plans import custom_open_plan_result
from api.services.open_plan_timeline import project_open_structured_plan
from api.services.plan_schedule import resolve_current_week
from api.services.streaks import qualifying_training_days
from api.services.today_service import _structured_day_for_training_day, resolve_training_day
from api.services.training_library import cache
from api.structured_plan_models import StructuredTrainingPlan
from tests.support import _build_client

AUTH = {"Authorization": "Bearer athlete-token"}
OTHER = {"Authorization": "Bearer admin-token"}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def body(weekday="Monday"):
    return {"name": "Athlete week", "sessions": [
        {"id": str(uuid4()), "name": "Strength", "weekday": weekday,
         "session_type": "strength_power", "exercises": [
             {"id": str(uuid4()), "origin": "manual", "name": "My squat",
              "notes": "Use my rack", "dose": {"mode": "reps", "sets": 3, "reps": 5,
              "load_or_intensity": "80 kg", "rest_seconds": 90, "tempo": "3-1-X", "per_side": True}},
         ]},
        {"id": str(uuid4()), "name": "Rounds", "weekday": weekday,
         "session_type": "conditioning", "exercises": [
             {"id": str(uuid4()), "origin": "manual", "name": "My bike",
              "dose": {"mode": "intervals", "rounds": 4, "work_seconds": 30, "rest_seconds": 0}},
         ]},
    ]}


@pytest.fixture
def api():
    client, store, _ = _build_client()
    store.create_intake("athlete-1", PlanRequest(athlete={"full_name": "Ari Mensah"}, no_scheduled_fight=True))
    return client, store


def publish(client, prescription):
    response = client.post("/api/custom-plans", headers=AUTH, json=prescription)
    assert response.status_code == 201, response.text
    draft = response.json()
    response = client.post(f"/api/custom-plans/{draft['id']}/open-plan", headers=AUTH)
    assert response.status_code == 200, response.text
    return draft, response.json()["plan_id"]


def current_plan(api):
    client, store = api
    day = resolve_training_day(None)
    weekday = WEEKDAYS[date.fromisoformat(day).weekday()]
    draft, plan_id = publish(client, body(weekday))
    row = store.plans[plan_id]
    # Keep the execution fixture in its current block even when run Fri-Sun.
    monday = date.fromisoformat(day) - timedelta(days=date.fromisoformat(day).weekday())
    row["created_at"] = f"{monday}T12:00:00+00:00"
    row["planning_brief"]["athlete_snapshot"]["plan_creation_weekday"] = "Mon"
    active = client.post(f"/api/plans/{plan_id}/set-active", headers=AUTH)
    assert active.status_code == 200, active.text
    return draft, row, day


def checkin(client, plan_id):
    response = client.post("/api/today/checkin", headers=AUTH, json={
        "plan_id": plan_id, "sleep": "good", "body": "normal", "pain": "none", "phase": "GPP",
    })
    assert response.status_code == 201, response.text


def complete(client, plan_id, session_id, status="started", **extra):
    response = client.post("/api/today/session-completion", headers=AUTH, json={
        "plan_id": plan_id, "session_id": session_id, "status": status, **extra,
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_publication_validates_exact_doses_and_uses_shared_detail(api):
    client, store = api
    draft, plan_id = publish(client, body())
    row = store.plans[plan_id]
    plan = StructuredTrainingPlan.model_validate(row["structured_plan"])
    assert row["fight_date"] is None and row["status"] == "ready"
    assert plan.plan_metadata.plan_type == "open_ongoing_system"
    assert store.get_active_plan_id("athlete-1") is None
    assert not store.generation_jobs
    first, second = plan.weeks[0].days[0].sessions
    assert first.blocks[0].athlete_dose == draft["sessions"][0]["exercises"][0]["dose"]
    assert first.blocks[0].rest.value == 90 and second.blocks[0].rest.value == 0
    assert first.blocks[0].prescription_text == "3 × 5 · 80 kg · rest 90s · tempo 3-1-X · per side"
    assert first.blocks[0].load.value == 80 and first.blocks[0].load.unit == "kg"
    assert first.blocks[0].notes == "Use my rack"
    assert not first.blocks[0].progression_rule and not first.blocks[0].why_today
    detail = client.get(f"/api/plans/{plan_id}", headers=AUTH)
    assert detail.status_code == 200, detail.text
    detail = detail.json()
    assert detail["custom_plan_id"] == draft["id"]
    summaries = client.get("/api/plans", headers=AUTH).json()
    assert summaries[0]["custom_plan_id"] == draft["id"]
    assert detail["structured_card_state"]["state"] == "live"
    assert detail["schedule_context"]["projection_status"] == "projected"
    assert len(detail["outputs"]["structured_plan"]["weeks"]) == 4


def test_publishing_first_custom_plan_activates_it_via_route(api):
    client, store = api

    def conditional(athlete_id, plan_id, expected_id, current_training_day=None):
        if store.get_active_plan_id(athlete_id) != expected_id:
            return False
        remaining = [row for row in store.list_user_plans(athlete_id) if row["status"] != "archived"]
        if len(remaining) != 1 or remaining[0]["id"] != plan_id:
            return False
        store.set_active_plan_id(athlete_id, plan_id)
        return True

    store.try_set_active_plan_id = conditional
    _, plan_id = publish(client, body())
    assert store.get_active_plan_id("athlete-1") == plan_id
    assert client.get("/api/plans/active", headers=AUTH).json()["plan_id"] == plan_id


def test_retry_has_one_published_plan_and_ownership_is_enforced(api):
    client, store = api
    draft, plan_id = publish(client, body())
    endpoint = f"/api/custom-plans/{draft['id']}/open-plan"
    assert client.post(endpoint, headers=AUTH).json()["plan_id"] == plan_id
    assert len(store.plans) == 1
    assert client.post(endpoint, headers=OTHER).status_code == 404
    assert client.post(endpoint).status_code == 401
    assert client.post(f"/api/plans/{plan_id}/set-active", headers=OTHER).status_code == 404


@pytest.mark.parametrize("missing", ["sessions", "weekday", "dose"])
def test_incomplete_drafts_save_but_cannot_execute(api, missing):
    client, store = api
    value = body()
    if missing == "sessions":
        value["sessions"] = []
    elif missing == "weekday":
        value["sessions"][0]["weekday"] = None
    else:
        value["sessions"][0]["exercises"][0]["dose"] = {}
    saved = client.post("/api/custom-plans", headers=AUTH, json=value)
    assert saved.status_code == 201
    assert client.post(f"/api/custom-plans/{saved.json()['id']}/open-plan", headers=AUTH).status_code == 422
    assert not store.plans


def test_sparring_and_support_on_one_day_project_without_duplicates(api):
    client, store = api
    value = body()
    value["sessions"].insert(0, {
        "id": str(uuid4()), "name": "Hard sparring", "weekday": "Monday",
        "session_type": "sparring", "exercises": [],
    })
    _, plan_id = publish(client, value)
    projected, context = project_open_structured_plan(store.plans[plan_id], store.plans[plan_id]["structured_plan"],
                                                     current_training_day="2026-11-09")
    assert context["projection_status"] == "projected"
    assert len(projected["weeks"][0]["days"][0]["sessions"]) == 3
    assert projected["weeks"][0]["days"][0]["today_card"]["coach_led_contact"] == "Hard sparring"


def test_shared_today_completion_logging_and_history(api):
    client, store = api
    _, row, day = current_plan(api)
    plan_id = row["id"]
    matched, _ = _structured_day_for_training_day(row, day)
    sessions = matched["sessions"]
    checkin(client, plan_id)
    today = client.get("/api/today", headers=AUTH)
    assert today.status_code == 200, today.text
    assert today.json()["today"]["next_session"]["session_id"] == sessions[0]["session_id"]
    assert today.json()["today"]["next_session"]["title"] == "Strength / Rounds"
    # Both Enhanced Card prescriptions belong to this combined day.
    detail = client.get(f"/api/plans/{plan_id}", headers=AUTH).json()
    detail_sessions = next(
        d["sessions"] for w in detail["outputs"]["structured_plan"]["weeks"] for d in w["days"]
        if d["date"] == day
    )
    assert detail_sessions == sessions
    complete(client, plan_id, sessions[0]["session_id"])
    for session in sessions:
        for block in session["blocks"]:
            logged = client.post("/api/today/exercise-log", headers=AUTH, json={
                "plan_id": plan_id, "block_id": block["block_id"], "status": "as_prescribed",
            })
            assert logged.status_code == 201, logged.text
            assert logged.json()["log"]["prescribed"]["athlete_dose"] == block["athlete_dose"]
            assert logged.json()["log"]["session_id"] == session["session_id"]
    complete(client, plan_id, sessions[0]["session_id"], "done")
    after = client.get("/api/today", headers=AUTH).json()
    assert after["today"]["completion_status"] == "done"
    assert after["today"]["next_session"]["calendar_date"] != day
    detail_completions = client.get(f"/api/plans/{plan_id}/completions", headers=AUTH).json()["completions"]
    assert {c["session_id"] for c in detail_completions if c["status"] == "done"} == {
        s["session_id"] for s in sessions
    }
    before = copy.deepcopy(store.xp_awards)
    complete(client, plan_id, sessions[1]["session_id"], "done")
    complete(client, plan_id, sessions[0]["session_id"], "done")
    completions = store.session_completions["athlete-1"]
    assert len(completions) == 2 and all(c["status"] == "done" for c in completions)
    assert len(store.exercise_logs) == 2
    assert store.xp_awards == before
    history = client.get("/api/today/session-completions", headers=AUTH).json()
    assert {c["session_title"] for c in history} == {"Strength", "Rounds"}


@pytest.mark.parametrize("elapsed_week", range(5))
@pytest.mark.parametrize("legacy_empty_block", [False, True])
def test_today_weekly_schedule_and_detail_share_four_week_cycle(api, elapsed_week, legacy_empty_block):
    client, store = api
    _, plan_id = publish(client, body())
    row = store.plans[plan_id]
    row["created_at"] = "2026-10-12T12:00:00Z"
    row["planning_brief"]["athlete_snapshot"]["plan_creation_weekday"] = "Mon"
    if legacy_empty_block:
        row["planning_brief"]["open_plan_spec"]["development_block"] = {}
    day = date(2026, 10, 12) + timedelta(weeks=elapsed_week)
    index, weekly = resolve_current_week(row, today=day)
    detail = _map_plan_detail(row, include_admin=False, current_training_day=day)
    context = detail.schedule_context
    assert weekly.week_count == 4
    assert index == weekly.week_index == elapsed_week % 4
    assert context.current_week_number == index + 1
    assert context.block_number == elapsed_week // 4 + 1
    assert weekly.day_label == f"Development week {context.current_week_number}"
    matched, structured_week = _structured_day_for_training_day(row, day.isoformat())
    assert structured_week["week_index"] == context.current_week_number
    assert matched["date"] == day.isoformat()
    assert matched["sessions"] == row["structured_plan"]["weeks"][0]["days"][0]["sessions"]


def test_renewal_preserves_weekdays_doses_ids_and_training_history(api):
    _, store = api
    profile = _map_profile_row(store.profiles["athlete-1"])
    draft = {**body(), "id": str(uuid4()), "incomplete": False}
    result = custom_open_plan_result(draft, profile, training_day="2026-10-12")
    row = {"id": result["plan_id"], "athlete_id": "athlete-1", "created_at": "2026-10-12T12:00:00Z", **result}
    store.plans[row["id"]] = row
    original = copy.deepcopy(row)
    p1, c1 = project_open_structured_plan(row, row["structured_plan"], current_training_day="2026-10-12")
    p2, c2 = project_open_structured_plan(row, row["structured_plan"], current_training_day="2026-11-09")
    assert (c1["block_number"], c2["block_number"]) == (1, 2)
    for old, new in zip(p1["weeks"], p2["weeks"]):
        assert old["days"][0]["weekday"] == new["days"][0]["weekday"] == "Mon"
        assert old["days"][0]["sessions"] == new["days"][0]["sessions"]
        assert date.fromisoformat(new["days"][0]["date"]).weekday() == 0
    store.upsert_session_completion("athlete-1", {
        "plan_id": row["id"], "session_id": p1["weeks"][0]["days"][0]["sessions"][0]["session_id"],
        "training_day": "2026-10-12", "status": "done",
    })
    qualified, _ = qualifying_training_days(store, athlete_id="athlete-1", today=date(2026, 11, 9))
    assert date(2026, 10, 12) in qualified
    assert row == original


def test_edits_keep_original_logs_and_cannot_replace_a_started_day(api):
    client, store = api
    draft, row, day = current_plan(api)
    old = copy.deepcopy(row)
    session = _structured_day_for_training_day(row, day)[0]["sessions"][0]
    checkin(client, row["id"])
    complete(client, row["id"], session["session_id"])
    saved_completions = copy.deepcopy(store.session_completions)
    value = body(WEEKDAYS[date.fromisoformat(day).weekday()])
    value["name"] = "Edited doses"
    assert client.put(f"/api/custom-plans/{draft['id']}", headers=AUTH, json=value).status_code == 200
    new_id = client.post(f"/api/custom-plans/{draft['id']}/open-plan", headers=AUTH).json()["plan_id"]
    assert new_id != row["id"] and store.plans[row["id"]] == old
    response = client.post(f"/api/plans/{new_id}/set-active", headers=AUTH, json={"overlap_action": "replace"})
    assert response.status_code == 409 and "next training day" in response.text
    assert store.get_active_plan_id("athlete-1") == row["id"]
    assert store.session_completions == saved_completions
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    set_active_plan(store, "athlete-1", new_id, overlap_action="replace", current_training_day=next_day)
    assert store.get_active_plan_id("athlete-1") == new_id
    assert store.plans[row["id"]]["status"] == "archived"


def test_injury_holds_athlete_prescription_without_substitution_or_added_rehab():
    session = {"session_id": "manual", "session_type": "strength_power", "prescription_source": "athlete",
               "blocks": [{"block_id": "lift", "block_type": "strength", "display_name": "My squat", "sets": 3}]}
    live = reconcile_session_prescription(session, plan_id="plan", training_day="2026-10-12", decisions=[{
        "activation": "live", "injury_id": "knee", "injury_episode_id": "episode",
        "outcome": "restricted", "restrictions": {"blocked_regions": ["knee"], "blocked_tags": [], "contact_limit": "full"},
    }])
    assert live["safety_hold"]
    assert live["session"]["blocks"][0]["display_name"] == "My squat"
    assert live["session"]["blocks"][0]["sets"] == 3
    assert all(c["action"] == "held" for c in live["changes"])


def test_catalogue_identity_and_media_survive_shared_mapper(api):
    client, store = api
    record = next(r for r in cache.get().records if r["summary"]["category"] == "training" and r["entry"]["exercise_key"])
    key = record["entry"]["exercise_key"]
    store.exercise_media[key] = {"exercise_key": key, "video_id": "abcdefghijk", "status": "ok", "source": "curated",
                                 "made_for_kids": False}
    from api.services.exercise_media import reset_media_index_cache
    reset_media_index_cache()
    value = body()
    value["sessions"][0]["exercises"][0] = {"id": str(uuid4()), "origin": "catalogue",
        "catalogue_id": record["entry"]["catalogue_id"], "dose": {"mode": "reps", "sets": 2, "reps": 6}}
    try:
        _, plan_id = publish(client, value)
        detail = client.get(f"/api/plans/{plan_id}", headers=AUTH).json()
        block = detail["outputs"]["structured_plan"]["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
        assert block["exercise_key"] == key and block["catalogue_id"] == record["entry"]["catalogue_id"]
        assert detail["outputs"]["exercise_media"][f"exercise:{key}"]["video_id"] == "abcdefghijk"
    finally:
        reset_media_index_cache()


def test_private_attachment_keeps_owner_scoped_reference_in_shared_cards(api):
    client, store = api
    media_id = str(uuid4())
    store.get_private_media = lambda owner, key: (
        {"id": media_id, "athlete_id": "athlete-1", "provider": "youtube", "video_id": "abcdefghijk",
         "start_s": 2, "end_s": 12} if owner == "athlete-1" and key == media_id else None
    )
    value = body()
    value["sessions"][0]["exercises"][0]["media_id"] = media_id
    _, plan_id = publish(client, value)
    detail = client.get(f"/api/plans/{plan_id}", headers=AUTH).json()
    block = detail["outputs"]["structured_plan"]["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    assert block["private_media_id"] == media_id
    assert "url" not in block and block["exercise_key"] is None
    assert client.post("/api/custom-plans", headers=OTHER, json=value).status_code == 404


@pytest.mark.parametrize("scopes", [["rehab"], ["rehab", "training"]])
def test_clinical_clearance_keeps_selected_contact_work_held(scopes):
    episode = str(uuid4())
    session = {"session_id": "sparring", "session_type": "sparring", "title": "Hard sparring",
               "prescription_source": "athlete", "blocks": [
                   {"block_id": "rounds", "block_type": "sparring", "display_name": "My rounds",
                    "rounds": 3, "work": {"value": 180, "unit": "seconds"}, "contact_level": "full"},
               ]}
    before = copy.deepcopy(session)
    live = reconcile_session_prescription(session, plan_id="plan", training_day="2026-10-12", decisions=[],
        injuries=[{"id": "injury", "episode_id": episode, "status": "open",
                   "clinician_clearance": {"episode_id": episode, "scopes": scopes}}])
    assert live["safety_hold"]
    assert live["session"]["blocks"][0]["display_name"] == "My rounds"
    assert live["session"]["blocks"][0]["rounds"] == 3
    assert session == before


def test_activation_refuses_corrupt_custom_structured_contract(api):
    client, store = api
    _, plan_id = publish(client, body())
    store.plans[plan_id]["structured_plan"]["plan_metadata"]["plan_type"] = "invalid"
    assert client.post(f"/api/plans/{plan_id}/set-active", headers=AUTH).status_code == 409
    assert store.get_active_plan_id("athlete-1") is None


def test_generated_cards_cannot_claim_athlete_authority_or_private_media(api):
    from api.services.effective_structured_plan import resolve_effective_structured_plan
    client, store = api
    _, plan_id = publish(client, body())
    row = copy.deepcopy(store.plans[plan_id])
    row["planning_brief"].pop("custom_plan")
    block = row["structured_plan"]["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    block["private_media_id"] = str(uuid4())
    resolved = resolve_effective_structured_plan(row)
    block = resolved["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    assert "prescription_source" not in resolved["plan_metadata"]
    assert "prescription_source" not in block and "private_media_id" not in block
    assert block["sets"] == 3 and block["load"]["value"] == 80


def test_readiness_and_severe_injury_still_gate_execution(api):
    client, store = api
    _, row, day = current_plan(api)
    session_id = _structured_day_for_training_day(row, day)[0]["sessions"][0]["session_id"]
    checkin(client, row["id"])
    store.create_injury_flag("athlete-1", {"body_area": "Left knee", "description": "Severe knee pain",
                                         "severity": "severe", "status": "open", "plan_id": row["id"]})
    response = client.post("/api/today/session-completion", headers=AUTH, json={
        "plan_id": row["id"], "session_id": session_id, "status": "started",
    })
    assert response.status_code == 409
    assert not store.session_completions.get("athlete-1")


@pytest.mark.parametrize("session_type", ["skill", "sparring"])
def test_coach_sessions_without_exercises_use_shared_today_actions(api, session_type):
    client, store = api
    day = resolve_training_day(None)
    value = body(WEEKDAYS[date.fromisoformat(day).weekday()])
    value["sessions"] = [{**value["sessions"][0], "name": "Coach session",
                          "session_type": session_type, "exercises": []}]
    _, plan_id = publish(client, value)
    row = store.plans[plan_id]
    monday = date.fromisoformat(day) - timedelta(days=date.fromisoformat(day).weekday())
    row["created_at"] = f"{monday}T12:00:00+00:00"
    row["planning_brief"]["athlete_snapshot"]["plan_creation_weekday"] = "Mon"
    assert client.post(f"/api/plans/{plan_id}/set-active", headers=AUTH).status_code == 200
    checkin(client, plan_id)
    today = client.get("/api/today", headers=AUTH).json()
    assert today["today"]["next_session"]["title"] == "Coach session"
    session_id = today["today"]["next_session"]["session_id"]
    complete(client, plan_id, session_id)
    complete(client, plan_id, session_id, "done")
    assert client.get("/api/today", headers=AUTH).json()["today"]["completion_status"] == "done"


def calendar_body():
    value = {"name": "Demo", "sessions": []}
    for day in ["Monday", "Wednesday", "Thursday", "Saturday"]:
        value["sessions"].append(body(day)["sessions"][0])
    value["sessions"] += [
        {"id": str(uuid4()), "name": "Training", "weekday": "Tuesday", "session_type": "recovery", "exercises": []},
        {"id": str(uuid4()), "name": "Training", "weekday": "Friday", "session_type": "sparring", "exercises": []},
    ]
    return value


@pytest.mark.parametrize("recovery_work", [False, True], ids=["empty-recovery", "optional-recovery-work"])
def test_json_text_custom_calendar_preserves_off_days_across_renewal(api, recovery_work):
    _, store = api
    value = calendar_body()
    if recovery_work:
        value["sessions"][-2]["exercises"] = body()["sessions"][0]["exercises"]
    profile = _map_profile_row(store.profiles["athlete-1"])
    result = custom_open_plan_result({**value, "id": str(uuid4())}, profile, training_day="2026-10-12")
    row = {"id": result["plan_id"], "created_at": "2026-10-12T12:00:00Z", **result}
    # Supabase can return the persisted brief as JSON text rather than a mapping.
    text_row = {**row, "planning_brief": json.dumps(row["planning_brief"])}
    original = copy.deepcopy(text_row)
    template_days = row["structured_plan"]["weeks"][0]["days"]

    for elapsed_week in (0, 3, 4, 7, 8):
        current_day = date(2026, 10, 12) + timedelta(weeks=elapsed_week)
        projected, context = project_open_structured_plan(
            text_row, text_row["structured_plan"], current_training_day=current_day.isoformat(),
        )
        assert context["projection_status"] == "projected"
        assert context["block_number"] == elapsed_week // 4 + 1
        assert context["current_week_number"] == elapsed_week % 4 + 1
        assert len(projected["weeks"]) == 4
        block_start = date(2026, 10, 12) + timedelta(weeks=(elapsed_week // 4) * 4)
        for week_position, week in enumerate(projected["weeks"]):
            assert [d["weekday"] for d in week["days"]] == [day[:3] for day in WEEKDAYS]
            for day_position, day in enumerate(week["days"]):
                assert day["date"] == (block_start + timedelta(weeks=week_position, days=day_position)).isoformat()
                # Exact session IDs, doses, notes and optional flags survive renewal.
                assert day["sessions"] == template_days[day_position]["sessions"]
            recovery, rest = week["days"][1], week["days"][6]
            assert rest["day_type"] == "rest" and rest["sessions"] == []
            assert recovery["day_type"] == "recovery"
            if recovery_work:
                assert len(recovery["sessions"]) == 1
                assert recovery["sessions"][0]["optional"] is True
                assert recovery["sessions"][0]["blocks"][0]["athlete_dose"]["load_or_intensity"] == "80 kg"
            else:
                assert recovery["sessions"] == []
        mapping_projection, mapping_context = project_open_structured_plan(
            row, row["structured_plan"], current_training_day=current_day.isoformat(),
        )
        assert projected == mapping_projection and context == mapping_context
    assert text_row == original


def test_demo_calendar_publishes_recovery_and_implicit_rest_without_data_loss(api):
    from api.services.today_service import _structured_today
    client, store = api
    value = calendar_body()
    draft, plan_id = publish(client, value)
    assert not draft["incomplete"]
    assert [s["name"] for s in draft["sessions"] if s["session_type"] == "sparring"] == ["Hard Sparring"]
    assert client.get(f"/api/custom-plans/{draft['id']}", headers=AUTH).json() == draft
    row = store.plans[plan_id]
    row["created_at"] = "2026-10-12T12:00:00Z"
    row["planning_brief"]["athlete_snapshot"]["plan_creation_weekday"] = "Mon"
    template = row["planning_brief"]["open_plan_spec"]["weekly_template"]
    assert template["training_days"] == ["Mon", "Wed", "Thu", "Fri", "Sat"]
    assert client.post(f"/api/plans/{plan_id}/set-active", headers=AUTH).status_code == 200
    detail = _map_plan_detail(row, include_admin=False, current_training_day=date(2026, 10, 12))
    for offset, kind in enumerate(["moderate", "recovery", "moderate", "moderate", "moderate", "moderate", "rest"]):
        day = (date(2026, 10, 12) + timedelta(days=offset)).isoformat()
        matched = _structured_day_for_training_day(row, day)[0]
        shown = next(d for d in detail.outputs.structured_plan.weeks[0].days if d.date == day)
        assert shown.day_type == matched["day_type"] == kind
        assert shown.model_dump(mode="json")["sessions"] == matched["sessions"]
        assert _structured_today(row, day).is_rest_day == (kind in {"recovery", "rest"})
        if kind in {"recovery", "rest"}:
            assert matched["sessions"] == []
    _, weekly = resolve_current_week(row, today=date(2026, 10, 13))
    assert weekly.days[1].title == "Recovery day" and weekly.days[1].effective_load == "none"
    assert weekly.days[6].effective_load == "none"
    for session in value["sessions"][:4]:
        saved = next(s for s in draft["sessions"] if s["id"] == session["id"])
        assert saved["exercises"][0]["notes"] == "Use my rack"
        assert saved["exercises"][0]["dose"]["load_or_intensity"] == "80 kg"


def test_saved_demo_with_stale_incomplete_flag_can_publish_and_saves_remain_idempotent(api):
    client, store = api
    value = calendar_body()
    saved = client.post("/api/custom-plans", headers=AUTH, json=value).json()
    store.custom_plans[saved["id"]]["incomplete"] = True
    for _ in range(3):
        response = client.post(f"/api/custom-plans/{saved['id']}/open-plan", headers=AUTH)
        assert response.status_code == 200, response.text
    assert len(store.plans) == 1
    assert store.custom_plans[saved["id"]]["sessions"] == saved["sessions"]
    assert not store.session_completions and not store.exercise_logs


@pytest.mark.parametrize("kind,with_work", [("rest", False), ("recovery", False), ("recovery", True)])
def test_off_days_cannot_create_workout_completions_or_xp(api, kind, with_work):
    client, store = api
    day = resolve_training_day(None)
    weekday = WEEKDAYS[date.fromisoformat(day).weekday()]
    value = body(WEEKDAYS[(date.fromisoformat(day).weekday() + 1) % 7])
    off = {"id": str(uuid4()), "name": "My downtime", "weekday": weekday, "session_type": kind,
           "exercises": body()["sessions"][0]["exercises"] if with_work else []}
    value["sessions"].append(off)
    _, plan_id = publish(client, value)
    row = store.plans[plan_id]
    monday = date.fromisoformat(day) - timedelta(days=date.fromisoformat(day).weekday())
    row["created_at"] = f"{monday}T12:00:00Z"
    row["planning_brief"]["athlete_snapshot"]["plan_creation_weekday"] = "Mon"
    assert client.post(f"/api/plans/{plan_id}/set-active", headers=AUTH).status_code == 200
    checkin(client, plan_id)
    matched, _ = _structured_day_for_training_day(row, day)
    if with_work:
        assert matched["sessions"][0]["optional"] is True
        detail = client.get(f"/api/plans/{plan_id}", headers=AUTH).json()
        shown = next(d for w in detail["outputs"]["structured_plan"]["weeks"] for d in w["days"] if d["date"] == day)
        assert shown["sessions"] == matched["sessions"]
    today = client.get("/api/today", headers=AUTH).json()["today"]
    assert today["next_session"].get("calendar_date") != day
    before = copy.deepcopy(store.xp_awards)
    for status in ["started", "done", "done"]:
        response = client.post("/api/today/session-completion", headers=AUTH, json={
            "plan_id": plan_id, "session_id": matched["sessions"][0]["session_id"] if with_work else off["id"], "status": status,
        })
        assert response.status_code == 409, response.text
    assert not store.session_completions.get("athlete-1")
    assert store.xp_awards == before
    qualified, _ = qualifying_training_days(store, athlete_id="athlete-1", today=date.fromisoformat(day))
    assert date.fromisoformat(day) not in qualified


def test_empty_strength_is_a_specific_publish_error_not_a_rest_day(api):
    client, store = api
    value = calendar_body()
    value["sessions"][1]["exercises"] = []
    saved = client.post("/api/custom-plans", headers=AUTH, json=value).json()
    assert saved["incomplete"]
    response = client.post(f"/api/custom-plans/{saved['id']}/open-plan", headers=AUTH)
    assert response.status_code == 422
    assert "Wednesday's Strength & Power session has no exercises" in response.json()["detail"]
    assert not store.plans



def test_round_doses_custom_names_and_occurrences_survive_repeated_saves(api):
    client, store = api
    value = calendar_body()
    sparring = value["sessions"][-1]
    sparring["name"] = "Friday gym rounds"
    sparring["exercises"] = [{"id": str(uuid4()), "origin": "manual", "name": "My rounds", "notes": "Coach's dose",
                              "dose": {"mode": "intervals", "rounds": 6, "work_seconds": 120, "rest_seconds": 45}}]
    draft, plan_id = publish(client, value)
    before = copy.deepcopy(draft["sessions"])
    for _ in range(3):
        saved = client.put(f"/api/custom-plans/{draft['id']}", headers=AUTH, json=value)
        assert saved.status_code == 200
        assert saved.json()["sessions"] == before
        assert client.post(f"/api/custom-plans/{draft['id']}/open-plan", headers=AUTH).json()["plan_id"] == plan_id
    assert len(store.plans) == 1
    block = next(d for d in store.plans[plan_id]["structured_plan"]["weeks"][0]["days"] if d["weekday"] == "Fri")["sessions"][0]["blocks"][0]
    assert block["rounds"] == 6 and block["work"]["value"] == 120 and block["rest"]["value"] == 45
    assert block["effort"] is None and block["intensity"] is None


def test_type_change_renames_only_defaults_on_save(api):
    client, _ = api
    value = calendar_body()
    value["sessions"][0]["name"] = "Lower Body"
    draft = client.post("/api/custom-plans", headers=AUTH, json=value).json()
    value["sessions"][0]["session_type"] = "conditioning"
    recovery = value["sessions"][-2]
    recovery.update(name="Recovery", session_type="strength_power", exercises=body()["sessions"][0]["exercises"])
    saved = client.put(f"/api/custom-plans/{draft['id']}", headers=AUTH, json=value).json()
    assert next(s for s in saved["sessions"] if s["id"] == value["sessions"][0]["id"])["name"] == "Lower Body"
    assert next(s for s in saved["sessions"] if s["id"] == recovery["id"])["name"] == "Strength & Power"


def test_switching_programmed_day_off_retains_work_without_prescribing_it(api):
    client, store = api
    value = calendar_body()
    original = copy.deepcopy(value["sessions"][0])
    value["sessions"][0]["session_type"] = "rest"
    draft, plan_id = publish(client, value)
    saved = next(s for s in draft["sessions"] if s["id"] == original["id"])
    assert saved["exercises"][0]["id"] == original["exercises"][0]["id"]
    assert saved["exercises"][0]["notes"] == original["exercises"][0]["notes"]
    monday = store.plans[plan_id]["structured_plan"]["weeks"][0]["days"][0]
    assert monday["day_type"] == "rest" and monday["sessions"] == []
    value["sessions"][0]["session_type"] = "strength_power"
    restored = client.put(f"/api/custom-plans/{draft['id']}", headers=AUTH, json=value).json()
    assert next(s for s in restored["sessions"] if s["id"] == original["id"])["exercises"] == saved["exercises"]



def test_recovery_work_cannot_be_retro_logged_for_xp(api):
    from datetime import datetime, timezone
    from fastapi import HTTPException
    from api.services.today_service import upsert_session_completion
    client, store = api
    value = calendar_body()
    value["sessions"][-2]["exercises"] = body()["sessions"][0]["exercises"]
    _, plan_id = publish(client, value)
    row = store.plans[plan_id]
    row["created_at"] = "2026-10-12T12:00:00Z"
    row["planning_brief"]["athlete_snapshot"]["plan_creation_weekday"] = "Mon"
    recovery = _structured_day_for_training_day(row, "2026-10-13")[0]["sessions"][0]
    before = copy.deepcopy(store.xp_awards)
    with pytest.raises(HTTPException) as rejected:
        upsert_session_completion(store, athlete_id="athlete-1", athlete_timezone="UTC",
            payload={"plan_id": plan_id, "session_id": recovery["session_id"], "status": "done", "training_day": "2026-10-13"},
            now=datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
    assert rejected.value.status_code == 422
    assert not store.session_completions.get("athlete-1") and store.xp_awards == before
