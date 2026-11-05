"""Athlete swaps, additions and removals on app-generated plans.

The generated card is never edited: changes live in plans.exercise_overrides
and are applied on read, so Plan Detail, Today, session start and exercise
logging all see the same effective workout.
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from uuid import uuid4

import pytest

from api.models import PlanRequest
from api.services.exercise_overrides import apply_exercise_overrides, override_block_id
from api.services.today_service import _structured_day_for_training_day, resolve_training_day
from tests.support import _build_client

AUTH = {"Authorization": "Bearer athlete-token"}
OTHER = {"Authorization": "Bearer admin-token"}
PLAN_ID = "33333333-3333-3333-3333-333333333333"
OPEN_PLAN_ID = "44444444-4444-4444-4444-444444444444"
SESSION_ID = "s-strength"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@pytest.fixture(autouse=True)
def no_clinical_policies(monkeypatch):
    monkeypatch.setattr("fightcamp.rehab_clinical.load_clinical_policies", lambda: ())
    monkeypatch.setattr("api.services.today_service.load_clinical_policies", lambda: ())


def back_squat(**extra) -> dict:
    return {
        "block_type": "strength", "display_name": "Back Squat", "exercise_key": "back-squat",
        "sets": 4, "reps": 5, "effort": {"method": "RPE", "value": 7},
        "rest": {"value": 180, "unit": "seconds"},
        "load": {"method": "absolute", "value": 100, "unit": "kg", "display": "100 kg"},
        "intensity": "100 kg",
        **extra,
    }


def plank(**extra) -> dict:
    return {"block_type": "accessory", "display_name": "Front Plank", "duration": {"value": 45, "unit": "seconds"},
            "sets": 3, **extra}


def rehab_block() -> dict:
    return {"block_type": "rehab", "display_name": "Ankle Alphabet", "block_id": "rehab-ankle",
            "duration": {"value": 120, "unit": "seconds"}}


def intake(store, **extra):
    request = PlanRequest(
        athlete={"full_name": "Ari Mensah", "technical_style": ["boxing"]}, no_scheduled_fight=True,
        equipment_access=extra.pop("equipment", ["barbell", "dumbbells", "kettlebell", "bands"]),
        key_goals=extra.pop("goals", ["strength"]), **extra,
    )
    return store.create_intake("athlete-1", request)


ANCHOR = {"intent": "Own each rep.", "focus_cue": "Brace", "reset_cue": "Breathe"}


def valid_card(weeks: list[dict]) -> dict:
    """Fill the display schema's required fields so Plan Detail renders the card."""
    for week in weeks:
        week.setdefault("week_goal", "Build strength")
        week.setdefault("start_date", "")
        week.setdefault("end_date", "")
        week.setdefault("load_focus", {k: "maintain" for k in ("volume", "intensity", "specificity", "fatigue_target")})
        week.setdefault("progression", {"week_type": "stabilise", "planned_change_from_previous": ""})
        for day in week["days"]:
            day.setdefault("day_type", "moderate")
            day.setdefault("countdown_label", "")
            day.setdefault("today_card", {"headline": "Strength", "readiness_status": "train_as_planned",
                                          "mindset_anchor": ANCHOR})
            for session in day["sessions"]:
                session.setdefault("objective", "Lower-body strength")
                session.setdefault("mindset_anchor", ANCHOR)
    return {
        "plan_metadata": {"title": "Camp", "sport": "boxing", "plan_type": "open_ongoing_system", "timezone": "UTC",
                          "status": "active", "created_by": "planner"},
        "athlete_context": {"athlete_id": "athlete-1", "sport_profile": "boxing"},
        "event_context": {"event_type": "none"},
        "weeks": weeks,
        "nutrition": {k: "" for k in ("summary", "daily_focus", "training_day_guidance", "fight_week_guidance")},
    }


def seed_dated(store, *, blocks=None, days_ahead=(0,), phase="GPP"):
    today = date.fromisoformat(resolve_training_day(None))
    row = intake(store)
    store.plans[PLAN_ID] = {
        "id": PLAN_ID, "athlete_id": "athlete-1", "intake_id": row["id"], "status": "ready",
        "plan_name": "Camp A", "created_at": f"{today - timedelta(days=3)}T00:00:00+00:00",
        "exercise_overrides": [], "exercise_overrides_revision": 0,
        "structured_plan": valid_card([{"week_index": 1, "phase_label": phase, "days": [
            {"date": (today + timedelta(days=offset)).isoformat(), "phase_label": phase, "sessions": [
                {"session_id": f"{SESSION_ID}-{offset}" if offset else SESSION_ID, "session_type": "strength_power",
                 "title": "Strength", "blocks": copy.deepcopy(blocks or [back_squat(), plank()])},
            ]} for offset in days_ahead
        ]}]),
    }
    store.set_active_plan_id("athlete-1", PLAN_ID)
    return today


def seed_open(store, *, blocks=None):
    """An AI open plan: one weekly template on today's weekday and the next."""
    today = date.fromisoformat(resolve_training_day(None))
    monday = today - timedelta(days=today.weekday())
    weekday = WEEKDAYS[today.weekday()]
    second = WEEKDAYS[(today.weekday() + 1) % 7]
    training_days = [d for d in WEEKDAYS if d in {weekday, second}]
    row = intake(store)
    template_days = []
    for day in training_days:
        template_days.append({"date": "", "weekday": day, "phase_label": "GPP", "day_type": "moderate", "sessions": [
            {"session_type": "strength_power", "title": "Strength" if day == weekday else "Power",
             "blocks": copy.deepcopy(blocks or [back_squat(), plank()]) if day == weekday else [plank()]},
        ]})
    store.plans[OPEN_PLAN_ID] = {
        "id": OPEN_PLAN_ID, "athlete_id": "athlete-1", "intake_id": row["id"], "status": "ready",
        "plan_name": "Open", "created_at": f"{monday}T12:00:00+00:00",
        "exercise_overrides": [], "exercise_overrides_revision": 0,
        "planning_brief": {
            "athlete_snapshot": {"plan_creation_weekday": "Mon", "plan_creation_weekday_basis": "athlete_local_weekday"},
            "open_plan_spec": {"plan_type": "open_ongoing_system", "weekly_template": {"training_days": training_days}},
        },
        "structured_plan": valid_card([{"week_index": 1, "phase_label": "GPP", "days": template_days}]),
    }
    store.set_active_plan_id("athlete-1", OPEN_PLAN_ID)
    return today


def day_blocks(store, plan_id, day, session_id=None):
    matched = _structured_day_for_training_day(store.plans[plan_id], day.isoformat() if isinstance(day, date) else day)
    assert matched is not None
    sessions = matched[0]["sessions"]
    session = next(s for s in sessions if session_id is None or s["session_id"] == session_id)
    return session, session["blocks"]


def detail_day(client, plan_id, day):
    detail = client.get(f"/api/plans/{plan_id}", headers=AUTH)
    assert detail.status_code == 200, detail.text
    return next(d for w in detail.json()["outputs"]["structured_plan"]["weeks"] for d in w["days"]
                if d["date"] == day.isoformat())


def squat_id(day):
    return f"blk-{day.isoformat()}-back-squat"


def options(client, plan_id, day, session_id, block_id, **params):
    return client.get(f"/api/plans/{plan_id}/exercise-changes/alternatives", headers=AUTH,
                      params={"date": day.isoformat(), "session_id": session_id, "block_id": block_id, **params})


def change(client, plan_id, **body):
    payload = {"client_request_id": str(uuid4()), **body}
    return client.post(f"/api/plans/{plan_id}/exercise-changes", headers=AUTH, json=payload), payload


# ---------------------------------------------------------------------------
# Alternatives
# ---------------------------------------------------------------------------


def test_alternatives_preserve_purpose_and_respect_equipment_and_metadata():
    client, store, _ = _build_client()
    today = seed_dated(store)
    response = options(client, PLAN_ID, today, SESSION_ID, squat_id(today))
    assert response.status_code == 200, response.text
    body = response.json()
    names = [a["name"] for a in body["alternatives"]]
    assert names, body
    assert "Back Squat" not in names and "Front Plank" not in names
    assert len(names) <= 5
    for alternative in body["alternatives"]:
        # Evidence-backed reasons only: every top pick names a real match.
        assert alternative["reason"]
    # Same squat pattern, from the verified bank, within the athlete's kit.
    assert "Cossack Squat" in names
    assert any("squat pattern" in (a["reason"] or "").lower() for a in body["alternatives"])
    # No pull-up bar / trap bar / sled options without that equipment.
    for alternative in body["alternatives"]:
        assert not any(e.lower() in {"trap bar", "sled", "pullup bar", "machine"} for e in alternative["equipment"] or [])
    assert body["scopes"] == [{"value": "occurrence", "label": "Just this workout"}]


def test_boxing_suggestions_and_explicit_library_writes_exclude_teep_drills():
    from api.services.training_library import cache

    client, store, _ = _build_client()
    today = seed_dated(store)
    assert additions(client, today, q="teep").json()["alternatives"] == []
    teep = next(r for r in cache.get().records if "Teep" in r["entry"]["name"] and r["summary"]["category"] == "training")
    response, _ = change(client, PLAN_ID, action="add", date=today.isoformat(), session_id=SESSION_ID,
                         catalogue_id=teep["entry"]["catalogue_id"], dose={"mode": "duration", "duration_seconds": 60})
    assert response.status_code == 409
    # A kickboxing athlete keeps kick drills; aliases use the same sport ontology.
    store.intakes["athlete-1"][-1]["intake"]["athlete"]["technical_style"] = ["Muay Thai"]
    assert additions(client, today, q="teep").json()["alternatives"]


def test_custom_plan_library_picker_is_sport_aware_and_keeps_general_training():
    client, store, _ = _build_client()
    store.profiles["athlete-1"]["technical_style"] = ["boxing"]
    assert not client.get("/api/training/library", headers=AUTH, params={"for_plan": True, "q": "teep"}).json()["entries"]
    assert client.get("/api/training/library", headers=AUTH, params={"for_plan": True, "q": "lunge"}).json()["entries"]


def test_name_word_matches_supply_options_for_a_legacy_lunge_without_catalogue_metadata():
    client, store, _ = _build_client()
    today = seed_dated(store, blocks=[back_squat(display_name="Reverse Lunge", exercise_key=None), plank()])
    _, blocks = day_blocks(store, PLAN_ID, today)
    response = options(client, PLAN_ID, today, SESSION_ID, blocks[0]["block_id"], all=True)
    assert response.status_code == 200, response.text
    matches = response.json()["alternatives"]
    assert matches and all("lunge" in a["name"].lower() for a in matches)
    assert all("Related lunge" in a["reason"] for a in matches)


@pytest.mark.parametrize("query,expected_mode", [("Tempo Hill Runs", "intervals"), ("plank", "duration"), ("sled push", "distance")])
def test_library_dose_fields_follow_the_drill(query, expected_mode):
    client, store, _ = _build_client()
    today = seed_dated(store)
    alternatives = additions(client, today, q=query).json()["alternatives"]
    assert any(a["dose_mode"] == expected_mode for a in alternatives), alternatives
    if query == "Tempo Hill Runs":
        pick = next(a for a in alternatives if a["name"] == query)
        assert pick["suggested_dose"] == {
            # #2913 publishes RPE 9 alongside the complete interval timings.
            "mode": "intervals", "work_seconds": 20, "rounds": 6, "rest_seconds": 60, "rpe": 9.0,
        }
        assert pick["dose_required"] is False  # Source now has a complete interval dose.


def test_added_exercise_can_be_edited_and_logged_with_its_updated_dose():
    client, store, _ = _build_client()
    today = seed_dated(store)
    original = copy.deepcopy(store.plans[PLAN_ID]["structured_plan"])
    pick = next(a for a in additions(client, today, q="lunge").json()["alternatives"] if a["dose_mode"] == "reps")
    added, _ = change(client, PLAN_ID, action="add", date=today.isoformat(), session_id=SESSION_ID,
                      catalogue_id=pick["catalogue_id"], dose={"mode": "reps", "sets": 2, "reps": 6})
    block_id = added.json()["session"]["blocks"][-1]["block_id"]
    edited, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                       block_id=block_id, dose={"mode": "reps", "sets": 3, "reps": 8, "rest_seconds": 75,
                       "load_or_intensity": "12.5 kg", "rpe": 6.5, "per_side": True},
                       coaching_cues=["Keep the front foot planted"], stop_rules=["Stop for sharp pain"])
    assert edited.status_code == 200, edited.text
    block = edited.json()["session"]["blocks"][-1]
    detail = detail_day(client, PLAN_ID, today)["sessions"][0]["blocks"][-1]
    assert detail == block
    assert block["sets"] == 3 and block["reps"] == 8 and block["load"]["value"] == 12.5
    assert block["rest"]["value"] == 75 and block["effort"]["value"] == 6.5
    assert store.plans[PLAN_ID]["structured_plan"] == original
    checkin = client.post("/api/today/checkin", headers=AUTH, json={"plan_id": PLAN_ID, "sleep": "good", "body": "normal", "pain": "none", "phase": "GPP"})
    assert checkin.status_code == 201
    assert client.post("/api/today/session-completion", headers=AUTH, json={"plan_id": PLAN_ID, "session_id": SESSION_ID, "status": "started"}).status_code == 201
    log = client.post("/api/today/exercise-log", headers=AUTH, json={"plan_id": PLAN_ID, "block_id": block["block_id"], "status": "as_prescribed"})
    assert log.status_code == 201, log.text
    assert log.json()["log"]["prescribed"]["load"]["value"] == 12.5
    locked, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                       block_id=block["block_id"], dose={"mode": "reps", "sets": 5, "reps": 10})
    assert locked.status_code == 409


def test_changing_format_removes_old_rep_and_weight_fields():
    client, store, _ = _build_client()
    today = seed_dated(store)
    edited, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                       block_id=squat_id(today), dose={"mode": "distance", "sets": 4, "distance": 20, "distance_unit": "m", "rest_seconds": 60})
    assert edited.status_code == 200, edited.text
    block = edited.json()["session"]["blocks"][0]
    assert block["distance"] == {"value": 20, "unit": "m"}
    assert not block.get("reps") and not block.get("load")


@pytest.mark.parametrize("kind,title", [("skill", "Technical Combat"), ("sparring", "Controlled fight-speed technical rounds"), (None, "Technical-only combat")])
def test_empty_technical_days_allow_sport_appropriate_additions(kind, title):
    client, store, _ = _build_client()
    today = seed_dated(store)
    original_day = store.plans[PLAN_ID]["structured_plan"]["weeks"][0]["days"][0]
    original_day["today_card"]["headline"] = title
    if kind:
        original_day["sessions"][0].update(session_type=kind, title=title, blocks=[])
        session_id = SESSION_ID
    else:
        original_day["sessions"] = []
        session_id = today.isoformat()
    original = copy.deepcopy(store.plans[PLAN_ID]["structured_plan"])
    found = client.get(f"/api/plans/{PLAN_ID}/exercise-changes/additions", headers=AUTH,
                       params={"date": today.isoformat(), "session_id": session_id})
    assert found.status_code == 200, found.text
    assert found.json()["alternatives"], found.text
    pick = found.json()["alternatives"][0]
    assert "teep" not in pick["name"].lower() and "kick" not in pick["name"].lower()
    response, _ = change(client, PLAN_ID, action="add", date=today.isoformat(), session_id=session_id,
                         catalogue_id=pick["catalogue_id"], dose={"mode": "duration", "duration_seconds": 120, "rpe": 4})
    assert response.status_code == 200, response.text
    added = response.json()["session"]["blocks"][0]
    assert added["display_name"] == pick["name"]
    assert detail_day(client, PLAN_ID, today)["sessions"][0]["blocks"][0] == added
    edited, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=session_id,
                       block_id=added["block_id"], dose={"mode": "duration", "duration_seconds": 180, "rpe": 5})
    assert edited.status_code == 200, edited.text
    assert edited.json()["session"]["blocks"][0]["duration"]["value"] == 180
    assert store.plans[PLAN_ID]["structured_plan"] == original


def test_equipment_mismatch_is_excluded():
    client, store, _ = _build_client()
    today = seed_dated(store)
    store.intakes["athlete-1"][-1]["intake"]["equipment_access"] = []
    body = options(client, PLAN_ID, today, SESSION_ID, squat_id(today)).json()
    from fightcamp.training_context import normalize_equipment_list
    from api.services.training_library import cache
    index = cache.get()
    for alternative in body["alternatives"]:
        raw = index.by_id[alternative["catalogue_id"]]["entry"]["raw"]
        assert set(normalize_equipment_list(raw.get("equipment"))) <= {"bodyweight", "none"}


def test_alternatives_never_raise_session_stress_or_phase():
    client, store, _ = _build_client()
    today = seed_dated(store)
    from api.services.training_library import cache
    index = cache.get()
    body = options(client, PLAN_ID, today, SESSION_ID, squat_id(today), all=True).json()
    ranks = {"none": 0, "low": 1, "moderate": 2, "high": 3}
    original = next(r for r in index.records if r["entry"]["exercise_key"] == "back-squat")["entry"]["raw"]
    for alternative in body["alternatives"]:
        raw = index.by_id[alternative["catalogue_id"]]["entry"]["raw"]
        assert "GPP" in raw["phases"]
        for field in ("cns_load", "movement_cost", "impact_cost"):
            assert ranks[raw[field]] <= ranks[original[field]], (alternative["name"], field)


def test_dose_carries_sets_reps_effort_but_never_absolute_load():
    client, store, _ = _build_client()
    today = seed_dated(store)
    body = options(client, PLAN_ID, today, SESSION_ID, squat_id(today)).json()
    cossack = next(a for a in body["alternatives"] if a["name"] == "Cossack Squat")
    assert cossack["prescription"] == "4 × 5 · RPE 7"
    response, _ = change(client, PLAN_ID, action="swap", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), catalogue_id=cossack["catalogue_id"])
    assert response.status_code == 200, response.text
    _, blocks = day_blocks(store, PLAN_ID, today)
    swapped = blocks[0]
    assert swapped["display_name"] == "Cossack Squat"
    assert swapped["exercise_key"] == "cossack-squat"
    assert (swapped["sets"], swapped["reps"]) == (4, 5)
    assert swapped["effort"] == {"method": "RPE", "value": 7}
    assert swapped["rest"] == {"value": 180, "unit": "seconds"}
    assert "load" not in swapped and "intensity" not in swapped
    assert swapped["athlete_change"]["original_name"] == "Back Squat"


def test_incompatible_interval_dose_is_not_copied():
    client, store, _ = _build_client()
    today = seed_dated(store, blocks=[back_squat(sets=None, reps=None, rounds=6, work={"value": 30, "unit": "seconds"}), plank()])
    body = options(client, PLAN_ID, today, SESSION_ID, squat_id(today)).json()
    assert body["alternatives"]
    for alternative in body["alternatives"]:
        # Rep work cannot inherit a 6 × 30s interval prescription.
        assert alternative["dose_required"] is True
        assert alternative["prescription"] is None
    pick = body["alternatives"][0]
    response, _ = change(client, PLAN_ID, action="swap", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), catalogue_id=pick["catalogue_id"])
    assert response.status_code == 422
    response, _ = change(client, PLAN_ID, action="swap", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), catalogue_id=pick["catalogue_id"],
                         dose={"mode": "reps", "sets": 3, "reps": 8})
    assert response.status_code == 200, response.text
    _, blocks = day_blocks(store, PLAN_ID, today)
    assert (blocks[0]["sets"], blocks[0]["reps"]) == (3, 8)
    assert "rounds" not in blocks[0] and "work" not in blocks[0]


def squat_hold(**extra) -> dict:
    return {"block_type": "strength", "display_name": "Squat Isometric", "exercise_key": "squat-isometric-110-1rm-parallel",
            "sets": 3, "duration": {"value": 6, "unit": "seconds"}, "rest": {"value": 120, "unit": "seconds"}, **extra}


def test_manual_swap_ignores_injury_restrictions():
    client, store, _ = _build_client()
    today = seed_dated(store, blocks=[squat_hold(), plank()])
    hold_id = f"blk-{today}-squat-isometric-110-1rm-parallel"
    healthy = options(client, PLAN_ID, today, SESSION_ID, hold_id, all=True).json()
    front = next(a for a in healthy["alternatives"] if a["exercise_key"] == "front-squat-isometric-105-1rm-100")
    store.create_injury_flag("athlete-1", {"body_area": "knee", "description": "knee pain on squats"})
    injured = options(client, PLAN_ID, today, SESSION_ID, hold_id, all=True).json()
    assert front["catalogue_id"] in {a["catalogue_id"] for a in injured["alternatives"]}
    response, _ = change(client, PLAN_ID, action="swap", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=hold_id, catalogue_id=front["catalogue_id"])
    assert response.status_code == 200, response.text


def test_manual_options_do_not_require_injury_guidance():
    client, store, _ = _build_client()
    today = seed_dated(store)

    def broken(*args, **kwargs):
        raise RuntimeError("injury store down")

    store.list_injury_flags = broken
    response = options(client, PLAN_ID, today, SESSION_ID, squat_id(today))
    assert response.status_code == 200, response.text


def test_unknown_or_unavailable_candidates_are_refused_server_side():
    client, store, _ = _build_client()
    today = seed_dated(store)
    from api.services.training_library import cache
    index = cache.get()
    trap = next(r for r in index.records if r["entry"]["exercise_key"] == "trap-bar-deadlift")
    response, _ = change(client, PLAN_ID, action="swap", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), catalogue_id=trap["entry"]["catalogue_id"])
    assert response.status_code == 409
    response, _ = change(client, PLAN_ID, action="swap", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), catalogue_id="exercise_bank.json:not-real")
    assert response.status_code == 409
    assert store.plans[PLAN_ID]["exercise_overrides"] == []


# ---------------------------------------------------------------------------
# Swap / remove / add / undo
# ---------------------------------------------------------------------------


def _cossack(client, today, plan_id=PLAN_ID, session_id=SESSION_ID, block_id=None):
    body = options(client, plan_id, today, session_id, block_id or squat_id(today)).json()
    return next(a for a in body["alternatives"] if a["name"] == "Cossack Squat")


def test_swap_is_idempotent_and_visible_identically_in_plan_detail_and_today():
    client, store, _ = _build_client()
    today = seed_dated(store)
    pick = _cossack(client, today)
    body = {"action": "swap", "date": today.isoformat(), "session_id": SESSION_ID,
            "block_id": squat_id(today), "catalogue_id": pick["catalogue_id"]}
    first, payload = change(client, PLAN_ID, **body)
    assert first.status_code == 200, first.text
    retry = client.post(f"/api/plans/{PLAN_ID}/exercise-changes", headers=AUTH, json=payload)
    assert retry.status_code == 200 and retry.json()["id"] == first.json()["id"]
    assert len(store.plans[PLAN_ID]["exercise_overrides"]) == 1
    assert store.plans[PLAN_ID]["exercise_overrides_revision"] == 1
    # The generated card is untouched.
    assert store.plans[PLAN_ID]["structured_plan"]["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["display_name"] == "Back Squat"
    session, blocks = day_blocks(store, PLAN_ID, today)
    detail = detail_day(client, PLAN_ID, today)["sessions"][0]
    assert [b["display_name"] for b in detail["blocks"]] == [b["display_name"] for b in blocks] == ["Cossack Squat", "Front Plank"]
    assert detail["blocks"][0]["block_id"] == blocks[0]["block_id"] == override_block_id(first.json()["id"])
    # Refresh: a fresh read still shows the change.
    assert detail_day(client, PLAN_ID, today)["sessions"][0]["blocks"][0]["display_name"] == "Cossack Squat"


def test_remove_and_undo():
    client, store, _ = _build_client()
    today = seed_dated(store, blocks=[back_squat(), plank(), plank(display_name="Side Plank", exercise_key="side-plank")])
    plank_id = f"blk-{today}-front-plank"
    response, _ = change(client, PLAN_ID, action="remove", date=today.isoformat(), session_id=SESSION_ID, block_id=plank_id)
    assert response.status_code == 200, response.text
    session, blocks = day_blocks(store, PLAN_ID, today)
    assert [b["display_name"] for b in blocks] == ["Back Squat", "Side Plank"]
    assert session["athlete_removed"][0]["display_name"] == "Front Plank"
    undo = client.post(f"/api/plans/{PLAN_ID}/exercise-changes/{response.json()['id']}/undo", headers=AUTH, json={})
    assert undo.status_code == 200, undo.text
    session, blocks = day_blocks(store, PLAN_ID, today)
    assert [b["display_name"] for b in blocks] == ["Back Squat", "Front Plank", "Side Plank"]
    assert "athlete_removed" not in session
    # Undo is idempotent and the entry is kept as history.
    again = client.post(f"/api/plans/{PLAN_ID}/exercise-changes/{response.json()['id']}/undo", headers=AUTH, json={})
    assert again.status_code == 200
    assert store.plans[PLAN_ID]["exercise_overrides"][0]["revoked_at"]


def test_removing_critical_work_asks_first_and_last_exercise_is_kept():
    client, store, _ = _build_client()
    today = seed_dated(store)
    response, _ = change(client, PLAN_ID, action="remove", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "confirm_remove"
    assert "only strength work" in response.json()["detail"]["message"]
    response, _ = change(client, PLAN_ID, action="remove", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), confirmed=True)
    assert response.status_code == 200, response.text
    response, _ = change(client, PLAN_ID, action="remove", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=f"blk-{today}-front-plank", confirmed=True)
    assert response.status_code == 409
    assert "at least one" in response.json()["detail"]


def test_add_inserts_one_exercise_without_duplicates():
    client, store, _ = _build_client()
    today = seed_dated(store)
    additions = client.get(f"/api/plans/{PLAN_ID}/exercise-changes/additions", headers=AUTH,
                           params={"date": today.isoformat(), "session_id": SESSION_ID})
    assert additions.status_code == 200, additions.text
    suggestions = additions.json()["alternatives"]
    assert suggestions
    assert not {s["name"] for s in suggestions} & {"Back Squat", "Front Plank"}
    pick = suggestions[0]
    dose = None if not pick["dose_required"] else (
        {"mode": "reps", "sets": 3, "reps": 8} if pick["dose_mode"] == "reps" else {"mode": "duration", "duration_seconds": 30})
    response, payload = change(client, PLAN_ID, action="add", date=today.isoformat(), session_id=SESSION_ID,
                               catalogue_id=pick["catalogue_id"], dose=dose)
    assert response.status_code == 200, response.text
    client.post(f"/api/plans/{PLAN_ID}/exercise-changes", headers=AUTH, json=payload)  # retried
    _, blocks = day_blocks(store, PLAN_ID, today)
    assert [b["display_name"] for b in blocks] == ["Back Squat", "Front Plank", pick["name"]]
    assert len({b["block_id"] for b in blocks}) == 3
    # The same exercise is not suggested twice once added.
    again = client.get(f"/api/plans/{PLAN_ID}/exercise-changes/additions", headers=AUTH,
                       params={"date": today.isoformat(), "session_id": SESSION_ID, "all": True}).json()
    assert pick["name"] not in {s["name"] for s in again["alternatives"]}


def additions(client, today, **params):
    return client.get(f"/api/plans/{PLAN_ID}/exercise-changes/additions", headers=AUTH,
                      params={"date": today.isoformat(), "session_id": SESSION_ID, **params})


def test_add_can_use_anything_from_the_library():
    client, store, _ = _build_client()
    today = seed_dated(store)
    # A footwork drill and sled work: outside the strength suggestions and
    # outside the athlete's equipment, but an explicit library pick.
    found = additions(client, today, q="sled push").json()
    sled = next(a for a in found["alternatives"] if "sled" in a["name"].lower())
    assert "Sled" in " ".join(sled["equipment"] or []) or sled["equipment"] is None
    footwork = additions(client, today, q="pivot").json()["alternatives"]
    assert footwork
    for pick in (sled, footwork[0]):
        dose = None if not pick["dose_required"] else (
            {"mode": "reps", "sets": 3, "reps": 6} if pick["dose_mode"] == "reps"
            else {"mode": "duration", "duration_seconds": 60} if pick["dose_mode"] == "duration"
            else {"mode": "intervals", "rounds": 3, "work_seconds": 30, "rest_seconds": 60})
        response, _ = change(client, PLAN_ID, action="add", date=today.isoformat(), session_id=SESSION_ID,
                             catalogue_id=pick["catalogue_id"], dose=dose)
        assert response.status_code == 200, response.text
    _, blocks = day_blocks(store, PLAN_ID, today)
    assert [b["display_name"] for b in blocks][-2:] == [sled["name"], footwork[0]["name"]]
    # Whatever its type, the athlete's own addition stays removable.
    added = blocks[-1]
    response, _ = change(client, PLAN_ID, action="remove", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=added["block_id"], confirmed=True)
    assert response.status_code == 200, response.text
    # Already in the workout: not offered again.
    again = additions(client, today, q="sled push").json()["alternatives"]
    assert sled["catalogue_id"] not in {a["catalogue_id"] for a in again}


def test_library_add_ignores_injury_restrictions():
    from api.services.training_library import cache

    client, store, _ = _build_client()
    today = seed_dated(store)
    store.create_injury_flag("athlete-1", {"body_area": "knee", "description": "knee pain on squats"})
    found = additions(client, today, q="front squat isometric").json()
    assert "front-squat-isometric-105-1rm-100" in {a["exercise_key"] for a in found["alternatives"]}
    assert not found["notice"]
    held = next(r for r in cache.get().records if r["entry"]["exercise_key"] == "front-squat-isometric-105-1rm-100")
    response, _ = change(client, PLAN_ID, action="add", date=today.isoformat(), session_id=SESSION_ID,
                         catalogue_id=held["entry"]["catalogue_id"], dose={"mode": "duration", "duration_seconds": 6})
    assert response.status_code == 200, response.text
    assert store.plans[PLAN_ID]["exercise_overrides"]


def test_rehab_and_clinical_blocks_stay_protected():
    client, store, _ = _build_client()
    today = seed_dated(store, blocks=[back_squat(), plank(), rehab_block()])
    response = options(client, PLAN_ID, today, SESSION_ID, "rehab-ankle")
    assert response.status_code == 409
    response, _ = change(client, PLAN_ID, action="remove", date=today.isoformat(), session_id=SESSION_ID,
                         block_id="rehab-ankle", confirmed=True)
    assert response.status_code == 409
    # A forged override entry cannot remove rehab on read either.
    store.plans[PLAN_ID]["exercise_overrides"] = [{
        "id": "forged", "action": "remove", "scope": "occurrence", "date": today.isoformat(),
        "session_id": SESSION_ID, "block_id": "rehab-ankle"}]
    _, blocks = day_blocks(store, PLAN_ID, today)
    assert "Ankle Alphabet" in [b["display_name"] for b in blocks]


def test_started_and_past_workouts_cannot_change():
    client, store, _ = _build_client()
    today = seed_dated(store)
    client.post("/api/today/checkin", headers=AUTH, json={
        "plan_id": PLAN_ID, "sleep": "good", "body": "normal", "pain": "none", "phase": "GPP"})
    pick = _cossack(client, today)
    start = client.post("/api/today/session-completion", headers=AUTH,
                        json={"plan_id": PLAN_ID, "session_id": SESSION_ID, "status": "started"})
    assert start.status_code == 201, start.text
    response, _ = change(client, PLAN_ID, action="swap", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), catalogue_id=pick["catalogue_id"])
    assert response.status_code == 409
    response, _ = change(client, PLAN_ID, action="swap", date=(today - timedelta(days=1)).isoformat(),
                         session_id=SESSION_ID, block_id=squat_id(today), catalogue_id=pick["catalogue_id"])
    assert response.status_code == 409


def test_logging_uses_the_effective_exercise():
    client, store, _ = _build_client()
    today = seed_dated(store)
    client.post("/api/today/checkin", headers=AUTH, json={
        "plan_id": PLAN_ID, "sleep": "good", "body": "normal", "pain": "none", "phase": "GPP"})
    pick = _cossack(client, today)
    response, _ = change(client, PLAN_ID, action="swap", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), catalogue_id=pick["catalogue_id"])
    new_id = override_block_id(response.json()["id"])
    start = client.post("/api/today/session-completion", headers=AUTH,
                        json={"plan_id": PLAN_ID, "session_id": SESSION_ID, "status": "started"})
    assert start.status_code == 201, start.text
    log = client.post("/api/today/exercise-log", headers=AUTH,
                      json={"plan_id": PLAN_ID, "block_id": new_id, "status": "as_prescribed"})
    assert log.status_code == 201, log.text
    assert log.json()["log"]["exercise_key"] == "cossack-squat"
    assert log.json()["log"]["prescribed"]["display_name"] == "Cossack Squat"
    stale = client.post("/api/today/exercise-log", headers=AUTH,
                        json={"plan_id": PLAN_ID, "block_id": squat_id(today), "status": "as_prescribed"})
    assert stale.status_code == 404
    done = client.post("/api/today/session-completion", headers=AUTH,
                       json={"plan_id": PLAN_ID, "session_id": SESSION_ID, "status": "done", "session_rpe": 7})
    assert done.status_code == 201, done.text
    # Once done, the occurrence is history: undo cannot rewrite it.
    undo = client.post(f"/api/plans/{PLAN_ID}/exercise-changes/{response.json()['id']}/undo", headers=AUTH, json={})
    assert undo.status_code == 409


def test_other_athletes_cannot_change_or_read_options():
    client, store, _ = _build_client()
    today = seed_dated(store)
    response = client.get(f"/api/plans/{PLAN_ID}/exercise-changes/alternatives", headers=OTHER,
                          params={"date": today.isoformat(), "session_id": SESSION_ID, "block_id": squat_id(today)})
    assert response.status_code == 404
    response = client.post(f"/api/plans/{PLAN_ID}/exercise-changes", headers=OTHER, json={
        "client_request_id": str(uuid4()), "action": "remove", "date": today.isoformat(),
        "session_id": SESSION_ID, "block_id": squat_id(today), "confirmed": True})
    assert response.status_code == 404
    assert store.plans[PLAN_ID]["exercise_overrides"] == []


def test_concurrent_write_is_detected_and_retried():
    client, store, _ = _build_client()
    today = seed_dated(store, blocks=[back_squat(), plank(), plank(display_name="Side Plank", exercise_key="side-plank")])
    original = store.compare_and_set_plan_exercise_overrides
    calls = {"n": 0}

    def racing(plan_id, athlete_id, *, expected_revision, overrides):
        calls["n"] += 1
        if calls["n"] == 1:
            # Another device wrote first.
            store.plans[plan_id]["exercise_overrides_revision"] += 1
            return None
        return original(plan_id, athlete_id, expected_revision=expected_revision, overrides=overrides)

    store.compare_and_set_plan_exercise_overrides = racing
    response, _ = change(client, PLAN_ID, action="remove", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=f"blk-{today}-side-plank")
    assert response.status_code == 200, response.text
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Scopes on open plans
# ---------------------------------------------------------------------------


def _open_ids(store, day):
    session, blocks = day_blocks(store, OPEN_PLAN_ID, day)
    return session["session_id"], next(b["block_id"] for b in blocks if b["display_name"] == "Back Squat")


def test_current_only_change_does_not_affect_future_occurrences():
    client, store, _ = _build_client()
    today = seed_open(store)
    session_id, block_id = _open_ids(store, today)
    body = options(client, OPEN_PLAN_ID, today, session_id, block_id).json()
    assert [s["value"] for s in body["scopes"]] == ["occurrence", "future"]
    pick = next(a for a in body["alternatives"] if a["name"] == "Cossack Squat")
    response, _ = change(client, OPEN_PLAN_ID, action="swap", date=today.isoformat(), session_id=session_id,
                         block_id=block_id, catalogue_id=pick["catalogue_id"])
    assert response.status_code == 200, response.text
    assert day_blocks(store, OPEN_PLAN_ID, today)[1][0]["display_name"] == "Cossack Squat"
    next_week = today + timedelta(days=7)
    assert day_blocks(store, OPEN_PLAN_ID, next_week)[1][0]["display_name"] == "Back Squat"


def test_future_change_applies_forward_and_restore_keeps_history():
    client, store, _ = _build_client()
    today = seed_open(store)
    session_id, block_id = _open_ids(store, today)
    pick = next(a for a in options(client, OPEN_PLAN_ID, today, session_id, block_id).json()["alternatives"]
                if a["name"] == "Cossack Squat")
    response, _ = change(client, OPEN_PLAN_ID, action="swap", scope="future", date=today.isoformat(),
                         session_id=session_id, block_id=block_id, catalogue_id=pick["catalogue_id"])
    assert response.status_code == 200, response.text
    for weeks in (0, 1, 2, 5):
        day = today + timedelta(days=7 * weeks)
        assert day_blocks(store, OPEN_PLAN_ID, day)[1][0]["display_name"] == "Cossack Squat", weeks
    # The other training day is a different workout: untouched.
    other = today + timedelta(days=1)
    assert [b["display_name"] for b in day_blocks(store, OPEN_PLAN_ID, other)[1]] == ["Front Plank"]
    # Complete today's workout, then restore from next week: today keeps the change.
    client.post("/api/today/checkin", headers=AUTH, json={
        "plan_id": OPEN_PLAN_ID, "sleep": "good", "body": "normal", "pain": "none", "phase": "GPP"})
    done = client.post("/api/today/session-completion", headers=AUTH, json={
        "plan_id": OPEN_PLAN_ID, "session_id": session_id, "status": "done", "session_rpe": 6})
    assert done.status_code == 201, done.text
    undo = client.post(f"/api/plans/{OPEN_PLAN_ID}/exercise-changes/{response.json()['id']}/undo",
                       headers=AUTH, json={})
    assert undo.status_code == 200, undo.text
    entry = store.plans[OPEN_PLAN_ID]["exercise_overrides"][0]
    assert entry["ends_before"] == (today + timedelta(days=1)).isoformat() and not entry.get("revoked_at")
    assert day_blocks(store, OPEN_PLAN_ID, today)[1][0]["display_name"] == "Cossack Squat"
    assert day_blocks(store, OPEN_PLAN_ID, today + timedelta(days=7))[1][0]["display_name"] == "Back Squat"


def test_future_edit_applies_on_the_same_weekday_and_reverts_forward():
    client, store, _ = _build_client()
    today = seed_open(store)
    session_id, block_id = _open_ids(store, today)

    def squat(day):
        return next(b for b in day_blocks(store, OPEN_PLAN_ID, day)[1] if b["display_name"] == "Back Squat")

    first, _ = change(client, OPEN_PLAN_ID, action="edit", scope="future", date=today.isoformat(),
                      session_id=session_id, block_id=block_id, dose={"mode": "reps", "sets": 3, "reps": 5},
                      coaching_cues=["Drive the floor away"])
    assert first.status_code == 200, first.text
    for weeks in (0, 1, 3):
        block = squat(today + timedelta(days=7 * weeks))
        assert (block["sets"], block["reps"], block["coaching_cues"]) == (3, 5, ["Drive the floor away"]), weeks
    # The other training day is a different workout: untouched.
    assert [b["display_name"] for b in day_blocks(store, OPEN_PLAN_ID, today + timedelta(days=1))[1]] == ["Front Plank"]
    # A second recurring edit stacks on the first; a one-off wins on its own day.
    second, _ = change(client, OPEN_PLAN_ID, action="edit", scope="future", date=today.isoformat(),
                       session_id=session_id, block_id=squat(today)["block_id"], dose={"mode": "reps", "sets": 2, "reps": 8})
    assert second.status_code == 200, second.text
    next_week = today + timedelta(days=7)
    one_off, _ = change(client, OPEN_PLAN_ID, action="edit", date=next_week.isoformat(),
                        session_id=day_blocks(store, OPEN_PLAN_ID, next_week)[0]["session_id"],
                        block_id=squat(next_week)["block_id"], dose={"mode": "reps", "sets": 5, "reps": 1})
    assert one_off.status_code == 200, one_off.text
    assert [(squat(today + timedelta(days=7 * w))["sets"], squat(today + timedelta(days=7 * w))["reps"])
            for w in (0, 1, 2)] == [(2, 8), (5, 1), (2, 8)]
    # Reverting the latest recurring edit from next week keeps it today and falls back to the first.
    undo = client.post(f"/api/plans/{OPEN_PLAN_ID}/exercise-changes/{second.json()['id']}/undo",
                       headers=AUTH, json={"date": (today + timedelta(days=14)).isoformat()})
    assert undo.status_code == 200, undo.text
    assert (squat(today)["sets"], squat(today)["reps"]) == (2, 8)
    assert (squat(today + timedelta(days=14))["sets"], squat(today + timedelta(days=14))["reps"]) == (3, 5)


def test_open_plan_detail_and_today_show_the_same_effective_workout():
    client, store, _ = _build_client()
    today = seed_open(store)
    session_id, block_id = _open_ids(store, today)
    pick = next(a for a in options(client, OPEN_PLAN_ID, today, session_id, block_id).json()["alternatives"]
                if a["name"] == "Cossack Squat")
    response, _ = change(client, OPEN_PLAN_ID, action="swap", scope="future", date=today.isoformat(),
                         session_id=session_id, block_id=block_id, catalogue_id=pick["catalogue_id"])
    assert response.status_code == 200, response.text
    for day in (today, today + timedelta(days=7)):
        detail = detail_day(client, OPEN_PLAN_ID, day)["sessions"][0]["blocks"]
        _, effective = day_blocks(store, OPEN_PLAN_ID, day)
        assert [b["display_name"] for b in detail] == [b["display_name"] for b in effective]
        assert [b["block_id"] for b in detail] == [b["block_id"] for b in effective]
        assert detail[0]["athlete_change"]["scope"] == "future"


def test_future_scope_is_refused_on_dated_camps():
    client, store, _ = _build_client()
    today = seed_dated(store)
    pick = _cossack(client, today)
    response, _ = change(client, PLAN_ID, action="swap", scope="future", date=today.isoformat(),
                         session_id=SESSION_ID, block_id=squat_id(today), catalogue_id=pick["catalogue_id"])
    assert response.status_code == 422


def test_custom_plans_share_the_same_override_reader():
    plan_row = {"planning_brief": {"custom_plan": {"id": "x"}}, "exercise_overrides": [{
        "id": "a", "action": "edit", "scope": "occurrence", "date": "2026-10-12", "session_id": "s", "block_id": "b",
        "block": {"block_type": "strength", "display_name": "X", "sets": 3, "reps": 8}}]}
    card = {"weeks": [{"days": [{"date": "2026-10-12", "sessions": [
        {"session_id": "s", "blocks": [{"block_id": "b", "block_type": "strength", "display_name": "X"}]}]}]}]}
    edited = apply_exercise_overrides(plan_row, card)
    assert edited["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["reps"] == 8
    assert "reps" not in card["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]


def test_generated_cards_cannot_claim_athlete_changes():
    from api.services.effective_structured_plan import resolve_effective_structured_plan

    today = date.fromisoformat(resolve_training_day(None))
    row = {"id": PLAN_ID, "structured_plan": {"weeks": [{"week_index": 1, "days": [{"date": today.isoformat(), "sessions": [
        {"session_id": "s", "session_type": "strength_power", "athlete_removed": [{"display_name": "Y"}],
         "blocks": [back_squat(athlete_change={"action": "swap"})]}]}]}]}}
    resolved = resolve_effective_structured_plan(row)
    session = resolved["weeks"][0]["days"][0]["sessions"][0]
    assert "athlete_removed" not in session and "athlete_change" not in session["blocks"][0]


@pytest.mark.parametrize("as_text", [False, True])
def test_custom_plan_api_allows_edits_without_mutating_the_original(as_text):
    client, store, _ = _build_client()
    today = seed_dated(store)
    brief = {"custom_plan": {"id": "athlete-built"}}
    store.plans[PLAN_ID]["planning_brief"] = json.dumps(brief) if as_text else brief
    original = copy.deepcopy(store.plans[PLAN_ID]["structured_plan"])
    assert options(client, PLAN_ID, today, SESSION_ID, squat_id(today)).status_code == 200
    assert additions(client, today).status_code == 200
    response, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), dose={"mode": "reps", "sets": 3, "reps": 8, "load_or_intensity": "85 kg"},
                         coaching_cues=["Brace before the rep"], stop_rules=["Stop if sharp pain"])
    assert response.status_code == 200, response.text
    edited = response.json()["session"]["blocks"][0]
    assert edited["load"]["value"] == 85 and edited["reps"] == 8
    assert edited["coaching_cues"] == ["Brace before the rep"]
    assert edited["stop_rules"] == ["Stop if sharp pain"]
    assert store.plans[PLAN_ID]["structured_plan"] == original


def test_edit_sets_and_clears_easier_and_progress():
    client, store, _ = _build_client()
    today = seed_dated(store)
    response, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), regression_options=[" Box squat ", ""],
                         progression_rule=" Add 2.5 kg when every rep is clean ")
    assert response.status_code == 200, response.text
    edited = response.json()["session"]["blocks"][0]
    assert edited["regression_options"] == ["Box squat"]
    assert edited["progression_rule"] == "Add 2.5 kg when every rep is clean"
    cleared, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                        block_id=edited["block_id"], regression_options=[], progression_rule="")
    assert cleared.status_code == 200, cleared.text
    block = cleared.json()["session"]["blocks"][0]
    assert block["regression_options"] == [] and not block.get("progression_rule")


def test_manual_swap_allows_unknown_loading_with_injury(monkeypatch):
    from api.services import exercise_customisation as service

    client, store, _ = _build_client()
    today = seed_dated(store)
    store.create_injury_flag("athlete-1", {"body_area": "shoulder", "description": "shoulder pain"})
    known = options(client, PLAN_ID, today, SESSION_ID, squat_id(today), all=True)
    assert known.status_code == 200, known.text
    assert known.json()["alternatives"]
    pick = known.json()["alternatives"][0]
    original = service._base_block

    def missing_metadata(record, *, block_type):
        block = original(record, block_type=block_type)
        if record["entry"]["catalogue_id"] == pick["catalogue_id"]:
            block.pop("mechanical_load_regions", None)
            block.pop("mechanical_risk_tags", None)
        return block

    # Select an option already cleared for this injury, so the missing
    # mechanical evidence is the only new reason to refuse it.
    flag = store.list_injury_flags("athlete-1")[0]
    store.update_injury_flag(flag["id"], {"status": "resolved"})
    monkeypatch.setattr(service, "_base_block", missing_metadata)
    # Metadata uncertainty does not bar a healthy athlete's choices.
    assert pick["catalogue_id"] in {a["catalogue_id"] for a in
                                    options(client, PLAN_ID, today, SESSION_ID, squat_id(today), all=True).json()["alternatives"]}
    store.create_injury_flag("athlete-1", {"body_area": "shoulder", "description": "shoulder pain"})
    response = options(client, PLAN_ID, today, SESSION_ID, squat_id(today), all=True)
    assert response.status_code == 200, response.text
    assert pick["catalogue_id"] in {a["catalogue_id"] for a in response.json()["alternatives"]}
    response, _ = change(client, PLAN_ID, action="swap", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), catalogue_id=pick["catalogue_id"])
    assert response.status_code == 200, response.text
    assert store.plans[PLAN_ID]["exercise_overrides"]


@pytest.mark.parametrize("target_position", [0, 1])
def test_recurring_change_targets_the_same_day_slot_with_duplicate_titles(target_position):
    client, store, _ = _build_client()
    today = seed_open(store)
    row = store.plans[OPEN_PLAN_ID]
    template_day = next(d for d in row["structured_plan"]["weeks"][0]["days"]
                        if d["weekday"] == WEEKDAYS[today.weekday()])
    template_day["sessions"] = [
        {**copy.deepcopy(template_day["sessions"][0]), "session_id": f"week-1-slot-{i}"} for i in range(2)
    ]
    # Four template weeks have identical titles/exercises but different ids.
    for week_index in range(2, 5):
        week = copy.deepcopy(row["structured_plan"]["weeks"][0])
        week["week_index"] = week_index
        for day in week["days"]:
            for i, session in enumerate(day["sessions"]):
                session["session_id"] = f"week-{week_index}-{day['weekday']}-slot-{i}"
        row["structured_plan"]["weeks"].append(week)
    original = copy.deepcopy(row["structured_plan"])
    matched = _structured_day_for_training_day(row, today.isoformat())[0]
    target = matched["sessions"][target_position]
    block_id = next(b["block_id"] for b in target["blocks"] if b["display_name"] == "Back Squat")
    pick = _cossack(client, today, OPEN_PLAN_ID, target["session_id"], block_id)
    response, _ = change(client, OPEN_PLAN_ID, action="swap", scope="future", date=today.isoformat(),
                         session_id=target["session_id"], block_id=block_id, catalogue_id=pick["catalogue_id"])
    assert response.status_code == 200, response.text
    for weeks in (0, 1, 2, 3, 5):
        day = today + timedelta(days=7 * weeks)
        sessions = _structured_day_for_training_day(row, day.isoformat())[0]["sessions"]
        assert len(sessions) == 2
        assert sessions[target_position]["blocks"][0]["display_name"] == "Cossack Squat"
        assert sessions[1 - target_position]["blocks"][0]["display_name"] == "Back Squat"
    assert row["structured_plan"] == original


def test_legacy_recurring_title_match_refuses_ambiguous_sessions():
    entry = {"id": "a", "action": "remove", "scope": "future", "date": "2026-10-12",
             "weekday": "Mon", "session_id": "old-week-id", "session_slot": "strength",
             "block_id": "old-block", "target_key": "back-squat"}
    card = {"weeks": [{"days": [{"date": "2026-10-19", "sessions": [
        {"session_id": f"new-{i}", "title": "Strength", "blocks": [back_squat(block_id=f"b-{i}")]} for i in range(2)
    ]}]}]}
    assert apply_exercise_overrides({"exercise_overrides": [entry]}, card) is card
    card["weeks"][0]["days"][0]["sessions"].pop()
    resolved = apply_exercise_overrides({"exercise_overrides": [entry]}, card)
    assert resolved["weeks"][0]["days"][0]["sessions"][0]["blocks"] == []


@pytest.mark.parametrize("dose", [None, {"mode": "reps", "sets": 4, "reps": 6, "load_or_intensity": "100 kg", "rest_seconds": 180}])
def test_edit_preserves_rir_and_unrepresented_prescription_metadata(dose):
    client, store, _ = _build_client()
    prescribed = back_squat(effort={"method": "RIR", "value": 3},
                           tempo={"eccentric": 3, "pause_bottom": 1, "concentric": "X"})
    today = seed_dated(store, blocks=[prescribed, plank()])
    before = detail_day(client, PLAN_ID, today)["sessions"][0]["blocks"][0]
    response, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), dose=dose, coaching_cues=["Brace before moving"])
    assert response.status_code == 200, response.text
    after = response.json()["session"]["blocks"][0]
    for field in ("effort", "tempo", "load", "rest"):
        assert after[field] == before[field]
    assert after["reps"] == (6 if dose else before["reps"])
    assert after["coaching_cues"] == ["Brace before moving"]


def test_edit_rechecks_new_injury_before_writing_load():
    client, store, _ = _build_client()
    today = seed_dated(store)
    store.create_injury_flag("athlete-1", {"body_area": "knee", "description": "knee pain on squats"})
    response, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), dose={"mode": "reps", "sets": 4, "reps": 5, "load_or_intensity": "120 kg"})
    assert response.status_code == 409, response.text
    assert store.plans[PLAN_ID]["exercise_overrides"] == []


def test_edit_fails_closed_when_current_injury_guidance_is_unavailable():
    client, store, _ = _build_client()
    today = seed_dated(store)

    def unavailable(*args, **kwargs):
        raise RuntimeError("injury guidance unavailable")

    store.list_injury_flags = unavailable
    response, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), dose={"mode": "reps", "sets": 4, "reps": 5, "load_or_intensity": "120 kg"})
    assert response.status_code == 503, response.text
    assert store.plans[PLAN_ID]["exercise_overrides"] == []


def test_edit_checks_requested_load_against_live_policy(monkeypatch):
    client, store, _ = _build_client()
    today = seed_dated(store)

    def load_ceiling(session, **kwargs):
        block = session["blocks"][0]
        return {"safety_hold": block.get("load", {}).get("value", 0) > 100,
                "session": session}

    monkeypatch.setattr("api.services.exercise_customisation.reconcile_session_prescription", load_ceiling)
    for weight, expected in [(120, 409), (95, 200)]:
        response, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                             block_id=squat_id(today), dose={"mode": "reps", "sets": 4, "reps": 5,
                                                            "load_or_intensity": f"{weight} kg"})
        assert response.status_code == expected, response.text
        if expected == 409:
            assert store.plans[PLAN_ID]["exercise_overrides"] == []


@pytest.mark.parametrize("dose", [
    {"mode": "reps", "sets": 2, "reps": 5, "load_or_intensity": "100 kg"},
    {"mode": "reps", "sets": 4, "reps": 3, "load_or_intensity": "100 kg"},
    {"mode": "reps", "sets": 4, "reps": 5, "load_or_intensity": "80 kg"},
    {"mode": "reps", "sets": 3, "reps": 4, "load_or_intensity": "70 kg", "rpe": 5.0},
])
def test_deload_is_never_held_by_injury_guidance(monkeypatch, dose):
    client, store, _ = _build_client()
    today = seed_dated(store)
    store.create_injury_flag("athlete-1", {"body_area": "knee", "description": "knee pain on squats"})

    def unavailable(*args, **kwargs):
        raise AssertionError("a deload needs no injury clearance")

    monkeypatch.setattr("api.services.exercise_customisation.SafetyContext", unavailable)
    response, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), dose=dose)
    assert response.status_code == 200, response.text


def test_heavier_edit_is_only_held_by_a_related_injury():
    client, store, _ = _build_client()
    today = seed_dated(store)
    store.create_injury_flag("athlete-1", {"body_area": "shoulder", "description": "sore shoulder"})
    heavier = {"mode": "reps", "sets": 5, "reps": 5, "load_or_intensity": "105 kg"}
    response, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), dose=heavier)
    assert response.status_code == 200, response.text


def test_dose_not_heavier_needs_every_quantity_comparable_and_no_higher():
    from api.services.exercise_customisation import _dose_not_heavier

    squat = back_squat()
    assert _dose_not_heavier(squat, back_squat(sets=3))
    assert _dose_not_heavier(squat, back_squat(reps="3-4"))
    assert not _dose_not_heavier(squat, back_squat(reps="4-6"))
    assert not _dose_not_heavier(squat, back_squat(load={"method": "absolute", "value": 230, "unit": "lb"}))
    assert _dose_not_heavier(squat, back_squat(load={"method": "absolute", "value": 200, "unit": "lb"}))
    assert not _dose_not_heavier(squat, back_squat(effort={"method": "RPE", "value": 8}))
    assert not _dose_not_heavier(squat, {**back_squat(), "duration": {"value": 30, "unit": "seconds"}})
    assert not _dose_not_heavier(plank(), plank(duration={"value": 2, "unit": "minutes"}))
    assert _dose_not_heavier(plank(), plank(duration={"value": 30, "unit": "seconds"}))
    jump = {"display_name": "Jump Squat", "sets": 4, "reps": 3}
    assert _dose_not_heavier(jump, {**jump, "sets": 3, "intensity": "Bodyweight"})
    assert not _dose_not_heavier(jump, {**jump, "sets": 3, "intensity": "weighted vest"})


def test_contraindications_only_count_for_an_injured_region():
    from api.services.exercise_customisation import SafetyContext

    safety = SafetyContext.__new__(SafetyContext)
    safety.has_injuries, safety.injured_regions = True, {"knee"}
    assert not safety.contraindicated({"contraindications": ["skip if shoulder symptoms are active"]})
    assert not safety.contraindicated({"contraindications": ["skip when pins or trap bar are unavailable"]})
    # Conditional warnings are decided by the generation guard (planner_excludes),
    # never by a region match here; an active-symptom note is absolute.
    assert not safety.contraindicated({"contraindications": ["skip if front-knee irritation increases"]})
    assert safety.contraindicated({"contraindications": ["avoid if knee symptoms are active"]})
    safety.injured_regions = set()
    assert safety.contraindicated({"contraindications": ["skip if shoulder symptoms are active"]})
    safety.has_injuries = False
    assert not safety.contraindicated({"contraindications": ["skip if front-knee irritation increases"]})


def test_switching_to_free_text_is_never_a_deload():
    from api.services.exercise_customisation import _dose_not_heavier, _edited_dose

    squat = back_squat()
    text = _edited_dose(copy.deepcopy(squat), {"mode": "text", "prescription": "max-effort jumps"})
    assert not _dose_not_heavier(squat, text)
    assert not _dose_not_heavier(squat, {**squat, "sets": 3, "athlete_dose": {"mode": "duration"}})
    same_text = {"display_name": "Shadowbox", "prescription_text": "3 easy rounds"}
    assert _dose_not_heavier(same_text, dict(same_text))
    assert not _dose_not_heavier(same_text, {**same_text, "prescription_text": "max-effort rounds"})


def test_free_text_edit_is_checked_against_injury_guidance():
    client, store, _ = _build_client()
    today = seed_dated(store)
    store.create_injury_flag("athlete-1", {"body_area": "knee", "description": "knee pain on squats"})
    response, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), dose={"mode": "text", "prescription": "max-effort jumps"})
    assert response.status_code == 409, response.text
    assert store.plans[PLAN_ID]["exercise_overrides"] == []


def test_shorter_rest_tempo_or_per_side_is_not_a_deload():
    from api.services.exercise_customisation import _dose_not_heavier

    squat = back_squat(rest={"value": 120, "unit": "seconds"})
    assert not _dose_not_heavier(squat, back_squat(sets=3, rest={"value": 30, "unit": "seconds"}))
    assert _dose_not_heavier(squat, back_squat(sets=3, rest={"value": 3, "unit": "minutes"}))
    assert _dose_not_heavier(squat, back_squat(sets=3, rest={"value": 2, "unit": "minutes"}))
    assert not _dose_not_heavier(back_squat(rest=None), back_squat(sets=3, rest={"value": 30, "unit": "seconds"}))
    assert not _dose_not_heavier(squat, back_squat(sets=3, rest={"value": 120, "unit": "seconds"}, tempo="5-2-1-0"))
    assert _dose_not_heavier(back_squat(tempo="3-1-X-0"), back_squat(sets=3, tempo="3-1-X-0"))
    assert not _dose_not_heavier(squat, back_squat(sets=2, rest={"value": 120, "unit": "seconds"},
                                                   athlete_dose={"mode": "reps", "per_side": True}))


def test_shorter_rest_edit_is_checked_against_injury_guidance():
    client, store, _ = _build_client()
    today = seed_dated(store)
    store.create_injury_flag("athlete-1", {"body_area": "knee", "description": "knee pain on squats"})
    response, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
                         block_id=squat_id(today), dose={"mode": "reps", "sets": 3, "reps": 5,
                                                        "load_or_intensity": "100 kg", "rest_seconds": 30})
    assert response.status_code == 409, response.text


def test_dose_edit_clears_inherited_adjustments_but_preserves_the_edited_prescription():
    client, store, _ = _build_client()
    today = seed_dated(store)
    row = store.plans[PLAN_ID]
    block = row["structured_plan"]["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    block.update(progression_rule="At the same dose, refine squat control.",
                 regression_options=["Use a shallower pain-free squat."])
    original = copy.deepcopy(row["structured_plan"])
    response, _ = change(client, PLAN_ID, action="edit", date=today.isoformat(), session_id=SESSION_ID,
        block_id=squat_id(today), dose={"mode": "reps", "sets": 2, "reps": 5, "load_or_intensity": "80 kg"})
    assert response.status_code == 200, response.text
    actual = response.json()["session"]["blocks"][0]
    assert actual["sets"] == 2 and actual["reps"] == 5 and actual["load"]["value"] == 80
    assert not actual.get("progression_rule") and not actual.get("regression_options")
    assert store.plans[PLAN_ID]["structured_plan"] == original
    override = store.plans[PLAN_ID]["exercise_overrides"][-1]["block"]
    assert not override.get("progression_rule") and not override.get("regression_options")


@pytest.mark.parametrize("action", ["add", "swap"])
def test_injury_projection_preserves_manual_choices(action):
    from api.contracts.injury_policy import reconcile_session_prescription

    manual = back_squat(block_id="manual", mechanical_load_regions=["knee"],
                        athlete_change={"action": action})
    generated = back_squat(block_id="generated", mechanical_load_regions=["knee"])
    snapshot = reconcile_session_prescription(
        {"session_id": SESSION_ID, "session_type": "strength_power", "blocks": [manual, generated]},
        decisions=[{"activation": "live", "injury_id": "knee", "outcome": "monitor",
                    "restrictions": {"blocked_regions": ["knee"], "contact_limit": "full"}}],
        plan_id=PLAN_ID, training_day="2026-10-24",
    )
    blocks = snapshot["session"]["blocks"]
    assert blocks[0] == manual
    assert blocks[1]["_policy_held"]
