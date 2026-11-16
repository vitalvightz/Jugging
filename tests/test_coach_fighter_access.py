"""Coach reads of a connected fighter: consent-bound, read-only, no private notes."""
from copy import deepcopy
from uuid import uuid4

import pytest

from api.auth import AuthenticatedUser
from tests.support import _build_client, grant_default_compliance

COACH, FIGHTER, STRANGER = (str(uuid4()) for _ in range(3))
PLAN, DAY, SECRET = str(uuid4()), "2026-10-10", "PRIVATE NOTE"
AS_COACH = {"Authorization": "Bearer coach-token"}
BASE = f"/api/coach/fighters/{FIGHTER}"
PATHS = ("/training/history", f"/training/history/unlxck/{uuid4()}", "/weight")


def session(title="Strength & Power", session_type="strength_power", block_type="strength", **changes):
    return {"athlete_id": FIGHTER, "plan_id": PLAN, "session_id": "s1", "training_day": DAY, "status": "done",
            "completion_origin": "reported", "session_rpe": 7, "pain_after": 3, "notes": SECRET,
            "prescription_snapshot": {"plan_id": PLAN, "training_day": DAY, "session": {
                "session_id": "s1", "title": title, "session_type": session_type,
                "blocks": [{"block_id": "squat", "block_type": block_type, "display_name": "Squat", "sets": 4, "reps": 6}]}},
            **changes}


@pytest.fixture
def camp():
    client, store, _ = _build_client()
    for token, user_id, role in (("coach-token", COACH, "coach"), ("fighter-token", FIGHTER, "athlete"),
                                 ("stranger-token", STRANGER, "coach")):
        user = AuthenticatedUser(user_id=user_id, email=f"{token}@example.com", full_name=token, metadata={})
        store.ensure_profile(user)
        store.profiles[user_id]["role"] = role
        client.app.state.auth_service.users_by_token[token] = user
    grant_default_compliance(store, FIGHTER)
    connection = {"id": str(uuid4()), "coach_id": COACH, "athlete_id": FIGHTER, "status": "accepted",
                  "share_recovery": True, "share_injuries": True, "share_weight": True}
    store.coach_connections.append(connection)
    return client, store, connection


def test_only_the_connected_coach_can_read_and_ending_the_connection_ends_access(camp):
    client, store, connection = camp
    for path in PATHS[::2]:
        assert client.get(BASE + path, headers=AS_COACH).status_code == 200
    for path in PATHS:
        assert client.get(BASE + path).status_code == 401
        # Another coach, the athlete acting as a coach, and an unconnected athlete id.
        assert client.get(BASE + path, headers={"Authorization": "Bearer stranger-token"}).status_code == 404
        assert client.get(BASE + path, headers={"Authorization": "Bearer fighter-token"}).status_code == 404
        assert client.get(f"/api/coach/fighters/{STRANGER}{path}", headers=AS_COACH).status_code == 404
    for status in ("pending", "revoked", "removed", "declined"):
        connection["status"] = status
        for path in PATHS:
            assert client.get(BASE + path, headers=AS_COACH).status_code == 404
    connection["status"] = "accepted"
    store.profiles[COACH]["role"] = "athlete"
    assert client.get(BASE + "/weight", headers=AS_COACH).status_code == 404


def test_history_and_details_show_recorded_work_without_private_notes(camp):
    client, store, _ = camp
    row = store.upsert_session_completion(FIGHTER, session())
    store.exercise_logs.append({"athlete_id": FIGHTER, "plan_id": PLAN, "session_id": "s1", "training_day": DAY,
        "block_id": "squat", "status": "modified", "notes": SECRET, "prescribed": {},
        "actual": {"sets": 3, "reps": 5, "load": {"value": 80, "unit": "kg"}, "effort": {"value": 8}}})
    spar = store.create_external_training_session(FIGHTER, {"training_day": DAY, "activity": "sparring",
        "duration_minutes": 40, "effort": "hard", "client_request_id": str(uuid4()), "request_fingerprint": "a" * 64})
    store.external_training_sessions[spar["id"]]["takeaway"] = SECRET
    store.sparring_logs.append({"id": str(uuid4()), "athlete_id": FIGHTER, "external_session_id": spar["id"],
        "training_day": DAY, "intensity": "hard", "rounds_completed": 6, "round_seconds": 180,
        "head_contact": "heavy", "rocked": True, "notes": SECRET})
    before = deepcopy((store.session_completions, store.external_training_sessions, store.exercise_logs))

    history = client.get(BASE + "/training/history", headers=AS_COACH)
    assert history.status_code == 200 and SECRET not in history.text
    plan = next(item for item in history.json()["sessions"] if item["source"] == "unlxck")
    assert plan["title"] == "Strength & Power" and plan["session_rpe"] == 7 and plan["pain_after"] == 3
    assert plan["history_editable"] is False and plan["notes"] is None

    details = client.get(f"{BASE}/training/history/unlxck/{row['id']}", headers=AS_COACH)
    assert details.status_code == 200 and SECRET not in details.text
    squat = details.json()["blocks"][0]
    assert squat["prescribed"]["sets"] == 4 and squat["actual"]["sets"] == 3
    assert squat["actual"]["load"] == {"value": 80, "unit": "kg"} or squat["actual"]["load"]["value"] == 80

    sparring = client.get(f"{BASE}/training/history/external/{spar['id']}", headers=AS_COACH)
    assert sparring.status_code == 200 and SECRET not in sparring.text
    report = sparring.json()["sparring"]
    assert report["rounds_completed"] == 6 and report["head_contact"] == "heavy" and report["rocked"] is True
    assert sparring.json()["session"]["duration_minutes"] == 40
    # Reads only.
    assert before == (store.session_completions, store.external_training_sessions, store.exercise_logs)
    for method in (client.post, client.put, client.patch, client.delete):
        assert method(f"{BASE}/training/history/unlxck/{row['id']}", headers=AS_COACH).status_code == 405


@pytest.mark.parametrize("withhold", ["connection", "athlete"])
def test_unshared_health_detail_stays_hidden(camp, withhold):
    client, store, connection = camp
    rehab = store.upsert_session_completion(FIGHTER, session("Knee rehab", "rehab", "rehab"))
    spar = store.create_external_training_session(FIGHTER, {"training_day": DAY, "activity": "sparring",
        "duration_minutes": 40, "effort": "hard", "client_request_id": str(uuid4()), "request_fingerprint": "a" * 64})
    store.sparring_logs.append({"id": str(uuid4()), "athlete_id": FIGHTER, "external_session_id": spar["id"],
        "training_day": DAY, "intensity": "hard", "rounds_completed": 6, "head_contact": "heavy", "rocked": True})
    if withhold == "connection":
        connection.update(share_recovery=False, share_injuries=False, share_weight=False)
    else:
        store.record_compliance_acceptance(FIGHTER, health_data_consent=False)

    history = client.get(BASE + "/training/history", headers=AS_COACH).json()["sessions"]
    plan = next(item for item in history if item["source"] == "unlxck")
    assert plan["title"] == "Programme session" and plan["pain_after"] is None
    details = client.get(f"{BASE}/training/history/unlxck/{rehab['id']}", headers=AS_COACH)
    assert "rehab" not in details.text.lower() and "health" not in details.text.lower()
    assert details.json()["blocks"] == [] and details.json()["session"]["pain_after"] is None
    report = client.get(f"{BASE}/training/history/external/{spar['id']}", headers=AS_COACH).json()["sparring"]
    assert report["rounds_completed"] == 6 and report["head_contact"] is None and report["rocked"] is None
    assert client.get(BASE + "/weight", headers=AS_COACH).json() == {
        "shared": False, "entries": [], "current_weight_kg": None, "target_weight_kg": None,
        "rolling_7_day_average_weight": None, "weekly_pace_kg": None}


def test_weight_shows_the_athletes_own_weigh_ins_and_target(camp):
    client, store, _ = camp
    as_fighter = {"Authorization": "Bearer fighter-token"}
    workspace = client.get("/api/nutrition/current", headers=as_fighter).json()
    entries = [{"date": "2026-10-08", "weight_kg": 74.2, "is_fasted": True, "notes": SECRET},
               {"date": "2026-10-10", "weight_kg": 73.6, "time": "07:10"}]
    saved = client.put("/api/nutrition/current", headers=as_fighter, json={
        **{key: workspace[key] for key in ("nutrition_profile", "s_and_c_preferences", "nutrition_readiness",
                                            "nutrition_coach_controls")},
        "shared_camp_context": {**workspace["shared_camp_context"], "target_weight_kg": 70},
        "nutrition_monitoring": {"daily_bodyweight_log": entries}})
    assert saved.status_code == 200, saved.text
    before = deepcopy((store.profiles[FIGHTER], store.intakes))

    weight = client.get(BASE + "/weight", headers=AS_COACH)
    assert weight.status_code == 200 and SECRET not in weight.text
    body = weight.json()
    assert body["shared"] is True and body["target_weight_kg"] == 70 and body["current_weight_kg"] == 73.6
    assert [(entry["date"], entry["weight_kg"]) for entry in body["entries"]] == [("2026-10-08", 74.2), ("2026-10-10", 73.6)]
    assert body["entries"][0]["is_fasted"] is True and body["entries"][1]["time"] == "07:10"
    assert before == (store.profiles[FIGHTER], store.intakes)


# --- Coach-recorded sessions -------------------------------------------------

BACK_SQUAT = "exercise_bank.json:1090492c-7713-4845-89fc-f311b9088445"
AS_FIGHTER = {"Authorization": "Bearer fighter-token"}
SESSIONS = BASE + "/training/sessions"


def boxing(**changes):
    return {"activity": "boxing", "duration_minutes": 60, "effort": "hard", "sport": "boxing", "session_type": "pads",
            "client_request_id": str(uuid4()), **changes}


def squat(session_id, headers=AS_COACH, base=SESSIONS):
    return {"url": f"{base}/{session_id}/exercises/{uuid4()}", "headers": headers, "json": {
        "exercise": {"catalogue_id": BACK_SQUAT}, "format": "strength",
        "actual": {"sets": 3, "reps": 5, "load": {"value": 100, "unit": "kg"}}}}


def test_a_coach_session_lands_in_the_fighters_history_attributed_and_without_xp(camp, monkeypatch):
    client, store, _ = camp
    awards = []
    monkeypatch.setattr("api.services.tracking_xp.reconcile_training_xp", lambda *args, **kwargs: awards.append(kwargs) or [])
    body = boxing()
    saved = client.post(SESSIONS, headers=AS_COACH, json=body)
    assert saved.status_code == 201, saved.text
    row = saved.json()
    assert row["logged_by_coach"] is True and row["logged_by_coach_name"] == "coach-token"
    assert row["duration_minutes"] == 60 and row["effort"] == "hard" and row["session_type"] == "pads"
    stored = store.external_training_sessions[row["id"]]
    assert stored["athlete_id"] == FIGHTER and stored["logged_by_coach_id"] == COACH
    # A retry, or a double tap, is the same session; a different body is refused.
    assert client.post(SESSIONS, headers=AS_COACH, json=body).json()["id"] == row["id"]
    assert client.post(SESSIONS, headers=AS_COACH, json={**body, "duration_minutes": 90}).status_code == 409
    assert len(store.external_training_sessions) == 1
    # The coach did not report it for the athlete: no XP, now or on a later exercise.
    assert client.put(**squat(row["id"])).status_code == 200
    assert awards == []

    theirs = client.get("/api/training/history?include_skipped=true&limit=200", headers=AS_FIGHTER).json()["sessions"]
    assert [(item["id"], item["logged_by_coach_name"]) for item in theirs] == [(row["id"], "coach-token")]
    details = client.get(f"/api/training/history/external/{row['id']}", headers=AS_FIGHTER).json()
    assert details["session"]["logged_by_coach"] is True
    assert details["blocks"][0]["actual"]["load"]["value"] == 100
    overview = client.get(BASE + "/training/history", headers=AS_COACH).json()["sessions"]
    assert overview[0]["id"] == row["id"] and overview[0]["logged_by_coach"] is True

    # The athlete's own log is unchanged: unattributed, and it still earns XP.
    own = client.post("/api/training/sessions", headers=AS_FIGHTER, json=boxing())
    assert own.status_code == 201 and own.json()["logged_by_coach"] is False and own.json()["logged_by_coach_name"] is None
    assert "logged_by_coach_id" not in store.external_training_sessions[own.json()["id"]]
    assert len(awards) == 1


def test_only_an_accepted_connection_can_write(camp):
    client, store, connection = camp
    for headers, path in ((None, SESSIONS), ({"Authorization": "Bearer stranger-token"}, SESSIONS),
                          (AS_FIGHTER, SESSIONS), (AS_COACH, f"/api/coach/fighters/{STRANGER}/training/sessions")):
        response = client.post(path, headers=headers, json=boxing())
        assert response.status_code == (401 if headers is None else 404)
    session_id = client.post(SESSIONS, headers=AS_COACH, json=boxing()).json()["id"]
    for status in ("pending", "revoked", "removed", "declined"):
        connection["status"] = status
        assert client.post(SESSIONS, headers=AS_COACH, json=boxing()).status_code == 404
        assert client.put(**squat(session_id)).status_code == 404
        assert client.get(f"{SESSIONS}/{session_id}/exercises", headers=AS_COACH).status_code == 404
    connection["status"] = "accepted"
    store.profiles[COACH]["role"] = "athlete"
    assert client.post(SESSIONS, headers=AS_COACH, json=boxing()).status_code == 404
    store.profiles[COACH]["role"] = "coach"
    # The athlete's own health-data consent gates logging for them too.
    store.record_compliance_acceptance(FIGHTER, health_data_consent=False)
    refused = client.post(SESSIONS, headers=AS_COACH, json=boxing())
    assert refused.status_code == 403 and "health-data" in refused.json()["detail"]
    assert client.put(**squat(session_id)).status_code == 403
    assert len(store.external_training_sessions) == 1 and store.manual_exercise_entries == []
    # Unknown fields, including someone else's identity, are rejected outright.
    store.record_compliance_acceptance(FIGHTER, health_data_consent=True)
    for extra in ({"athlete_id": STRANGER}, {"logged_by_coach_id": STRANGER}, {"training_day": "2999-01-01"}):
        assert client.post(SESSIONS, headers=AS_COACH, json=boxing(**extra)).status_code == 422


def test_a_coach_can_only_change_sessions_they_recorded(camp):
    client, store, _ = camp
    store.coach_connections.append({"id": str(uuid4()), "coach_id": STRANGER, "athlete_id": FIGHTER,
                                    "status": "accepted", "share_recovery": True, "share_injuries": True})
    as_other = {"Authorization": "Bearer stranger-token"}
    own = client.post("/api/training/sessions", headers=AS_FIGHTER, json=boxing()).json()["id"]
    mine = client.post(SESSIONS, headers=AS_COACH, json=boxing()).json()["id"]
    assert client.put(**squat(mine)).status_code == 200
    # Not the athlete's own session, and not another connected coach's.
    assert client.put(**squat(own)).status_code == 404
    assert client.get(f"{SESSIONS}/{own}/exercises", headers=AS_COACH).status_code == 404
    assert client.put(**squat(mine, headers=as_other)).status_code == 404
    entry = client.get(f"{SESSIONS}/{mine}/exercises", headers=AS_COACH).json()["entries"][0]["entry_id"]
    assert client.delete(f"{SESSIONS}/{mine}/exercises/{entry}", headers=as_other).status_code == 404
    # No coach route edits or deletes a session; the record stays the athlete's.
    for method in (client.patch, client.delete, client.put):
        assert method(f"{SESSIONS}/{mine}", headers=AS_COACH).status_code in {404, 405}
        assert method(f"{SESSIONS}/{own}", headers=AS_COACH).status_code in {404, 405}
    assert client.delete(f"{SESSIONS}/{mine}/exercises/{entry}", headers=AS_COACH).status_code == 204
    # The athlete keeps full control of it.
    assert client.delete(f"/api/training/sessions/{mine}", headers=AS_FIGHTER).status_code == 204
    assert client.put(**squat(mine)).status_code == 404


def test_a_connection_accepted_before_weight_sharing_does_not_expose_weight(camp):
    client, store, connection = camp
    # Accepted under the earlier wording: recovery and injuries, never weight.
    connection.pop("share_weight")
    assert client.get(BASE + "/weight", headers=AS_COACH).json()["shared"] is False
    connection["share_weight"] = False
    assert client.get(BASE + "/weight", headers=AS_COACH).json()["shared"] is False
    # Recovery and injury sharing are unaffected; training is still shared.
    assert client.get(BASE + "/training/history", headers=AS_COACH).status_code == 200
    connection["share_weight"] = True
    assert client.get(BASE + "/weight", headers=AS_COACH).json()["shared"] is True
    store.record_compliance_acceptance(FIGHTER, health_data_consent=False)
    assert client.get(BASE + "/weight", headers=AS_COACH).json()["shared"] is False


def mixed(session_id, title, blocks):
    row = session(title, "strength_power", session_id=session_id)
    row["prescription_snapshot"]["session"].update(session_id=session_id, blocks=blocks)
    return row


SQUAT = {"block_id": "squat", "block_type": "strength", "display_name": "Squat", "sets": 4, "reps": 6}


@pytest.mark.parametrize("shared", [False, True])
def test_mixed_rehab_sessions_reveal_no_injury_detail_unless_shared(camp, shared):
    client, store, connection = camp
    connection["share_injuries"] = shared
    titled = store.upsert_session_completion(FIGHTER, mixed("s1", "Knee Rehab + Strength", [SQUAT]))
    carrying = store.upsert_session_completion(FIGHTER, mixed("s2", "Lower Body", [
        SQUAT, {"block_id": "knee", "block_type": "rehab", "display_name": "Knee isometric hold", "sets": 3}]))
    named = store.upsert_session_completion(FIGHTER, mixed("s3", "Lower Body B", [
        SQUAT, {"block_id": "band", "block_type": "strength", "display_name": "Hamstring strain prehab", "sets": 2}]))
    plain = store.upsert_session_completion(FIGHTER, mixed("s4", "Power Day", [SQUAT]))

    history = client.get(BASE + "/training/history", headers=AS_COACH)
    titles = {row["id"]: row["title"] for row in history.json()["sessions"]}
    for row, title in ((titled, "Knee Rehab + Strength"), (carrying, "Lower Body"), (named, "Lower Body B")):
        details = client.get(f"{BASE}/training/history/unlxck/{row['id']}", headers=AS_COACH)
        names = [block["name"] for block in details.json()["blocks"]]
        if shared:
            assert titles[row["id"]] == title and details.json()["session"]["title"] == title
        else:
            assert "Squat" in names, "the training work itself stays visible"
            for text in (history.text, details.text):
                for word in ("Knee", "Rehab", "rehab", "strain", "prehab", "Hamstring"):
                    assert word not in text
    if not shared:
        assert titles[titled["id"]] == titles[carrying["id"]] == "Programme session"
    else:
        named_details = client.get(f"{BASE}/training/history/unlxck/{named['id']}", headers=AS_COACH).json()
        assert "Hamstring strain prehab" in [block["name"] for block in named_details["blocks"]]
    assert titles[plain["id"]] == "Power Day"
