"""AI training focus inferred from notes the athlete already writes.

The model is always mocked. Covers the three sources, day context, grounding,
explicit-focus priority, edits, deletion, duplicate and stale calls, failure,
consent and health language.
"""
import json
from uuid import uuid4

import pytest

from api.contracts.training_focus import InferredFocus, movement_pattern, response_schema
from api.routes import today as today_routes
from api.services import training_focus_inference as inference
from api.services.today_service import resolve_training_day
from tests.support import _build_client, withdraw_health_consent

AUTH = {"Authorization": "Bearer athlete-token"}
ATHLETE_ID = "athlete-1"
ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


class FakeInferrer:
    model = "gpt-6-luna"

    def __init__(self, answer=None, *, error: Exception | None = None, during=None):
        self.answer = answer
        self.error = error
        self.during = during
        self.payloads: list[dict] = []

    def infer(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        if self.during:
            during, self.during = self.during, None
            during()
        if self.error:
            raise self.error
        answer = self.answer(payload) if callable(self.answer) else self.answer
        return inference.FocusModelResult(
            inferred=InferredFocus(**answer) if answer is not None else None, input_tokens=420, output_tokens=38)


@pytest.fixture
def api(monkeypatch):
    client, store, _ = _build_client()
    fake = FakeInferrer()
    monkeypatch.setattr(inference, "get_focus_inferrer", lambda: fake)
    return client, store, fake


def today():
    return resolve_training_day(None)


def read_items(client):
    response = client.get("/api/today/training-focus", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def lower_body_completion(store, *, notes="", day=None, exercise_logs=None, session_id="s-lower",
                          session_type="strength"):
    """A finished plan session with a frozen prescription and optional results."""
    day = day or today()
    plan_id = str(uuid4())
    blocks = [
        {"block_id": "b1", "block_type": "strength", "display_name": "Split Squat", "sets": 3, "reps": "8"},
        {"block_id": "b2", "block_type": "strength", "display_name": "Romanian Deadlift (RDL)", "sets": 3, "reps": "6"},
        {"block_id": "b3", "block_type": "strength", "display_name": "Calf Raises", "sets": 2, "reps": "15"},
    ]
    row = store.upsert_session_completion(ATHLETE_ID, {
        "plan_id": plan_id, "session_id": session_id, "training_day": day, "status": "done",
        "completion_origin": "reported", "notes": notes, "completed_at": f"{day}T10:00:00+00:00",
        "prescription_snapshot": {"plan_id": plan_id, "training_day": day, "session": {
            "session_id": session_id, "title": "Lower-body strength", "session_type": session_type, "blocks": blocks}},
    })
    for block_id, status, actual in exercise_logs or []:
        store.exercise_logs.append({"id": str(uuid4()), "athlete_id": ATHLETE_ID, "plan_id": plan_id,
                                    "training_day": day, "session_id": session_id, "block_id": block_id,
                                    "status": status, "actual": actual, "created_at": f"{day}T10:00:00+00:00"})
    return row


def edit_notes(client, completion_id, notes):
    response = client.patch(f"/api/training/history/unlxck/{completion_id}", headers=AUTH, json={"notes": notes})
    assert response.status_code == 200, response.text
    return response


def manual_session(client, **fields):
    body = {"activity": "strength", "duration_minutes": 45, "effort": "moderate", "client_request_id": str(uuid4()),
            **fields}
    response = client.post("/api/training/sessions", headers=AUTH, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def save_takeaway(client, entry_id, text):
    response = client.put(f"/api/training/sessions/{entry_id}/takeaway", headers=AUTH, json={"takeaway": text})
    assert response.status_code == 200, response.text


def sparring_detail(client, entry_id, **fields):
    body = {"plan_id": None, "source": "free", "external_session_id": entry_id, "client_request_id": str(uuid4()),
            "detail_revision": 1, "intensity": "medium", "rounds_completed": 4, "head_contact": "light",
            "rocked": False, "notes": "", "focus": "", **fields}
    response = client.post("/api/today/sparring-log", headers=AUTH, json=body)
    assert response.status_code == 201, response.text
    return response.json()["log"]


SPLIT_SQUAT = {"focus": "Stay balanced and controlled through each split squat.", "scope": "specific_exercise",
               "category": "strength", "sport": None, "exercise_targets": ["split squat"], "movement_patterns": []}


# -- Plan completion notes ------------------------------------------------------


def test_plan_note_resolves_the_first_exercise_from_the_session_context(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "I struggled with balance on the first exercise.")
    assert len(fake.payloads) == 1
    payload = fake.payloads[0]
    noted = payload["context"]["noted_session"]
    assert payload["source"] == "plan_completion" and payload["note"] == "I struggled with balance on the first exercise."
    assert noted["title"] == "Lower-body strength" and noted["session_type"] == "strength"
    assert [item["name"] for item in noted["exercises"]] == ["Split Squat", "Romanian Deadlift (RDL)", "Calf Raises"]
    assert [item.get("pattern") for item in noted["exercises"]] == ["unilateral_squat", "hinge", "calf"]
    assert payload["context"]["categories"] == ["strength"]
    # Minimum necessary: no identity, profile or other history goes out.
    sent = json.dumps(payload)
    assert "athlete-1" not in sent and "ari@example.com" not in sent and "Ari" not in sent
    [item] = read_items(client)
    assert item["origin"] == "inferred" and item["focus"] == SPLIT_SQUAT["focus"]
    assert item["scope"] == "specific_exercise" and item["exercise_targets"] == ["split squat"]
    assert item["movement_patterns"] == []
    stored = store.training_focus_inferences[0]
    assert stored["model"] == "gpt-6-luna" and stored["input_tokens"] == 420 and stored["output_tokens"] == 38
    # The athlete's note is never rewritten.
    assert store.get_session_completion_by_id(ATHLETE_ID, row["id"])["notes"] == "I struggled with balance on the first exercise."


def test_prescribed_work_that_was_skipped_or_replaced_is_marked_as_not_done(api):
    client, store, fake = api
    fake.answer = None
    row = lower_body_completion(store, exercise_logs=[
        ("b1", "skipped", {}), ("b2", "modified", {"sets": 2, "reps": 6}), ("b3", "as_prescribed", {"sets": 2, "reps": 15})])
    edit_notes(client, row["id"], "Balance was off on the first one.")
    exercises = fake.payloads[0]["context"]["noted_session"]["exercises"]
    assert [item["status"] for item in exercises] == ["skipped", "modified", "done"]
    assert exercises[1]["prescribed"] == {"sets": 3, "reps": "6"} and exercises[1]["actual"] == {"sets": 2, "reps": 6}


def test_completion_save_on_today_schedules_inference(api, monkeypatch):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store, notes="Split squats felt wobbly, I kept losing balance.")
    monkeypatch.setattr(today_routes, "upsert_session_completion", lambda *args, **kwargs: dict(row))
    response = client.post("/api/today/session-completion", headers=AUTH,
                           json={"plan_id": row["plan_id"], "session_id": "s-lower", "status": "done",
                                 "notes": "Split squats felt wobbly, I kept losing balance."})
    assert response.status_code == 201, response.text
    assert len(fake.payloads) == 1
    assert read_items(client)[0]["exercise_targets"] == ["split squat"]


def test_note_about_another_session_that_day_is_not_grounded(api):
    client, store, fake = api
    row = lower_body_completion(store)
    boxing = manual_session(client, activity="boxing", sport="boxing", session_type="pads")
    # The model points at the jab from the other session: not in the noted session, so nothing is kept.
    fake.answer = {"focus": "Bring the jab straight back to guard.", "scope": "specific_exercise",
                   "category": "strength", "sport": None, "exercise_targets": ["jab"], "movement_patterns": []}
    edit_notes(client, row["id"], "Jab kept dropping.")
    others = fake.payloads[0]["context"]["other_sessions_same_day"]
    assert others == [{"kind": "manual", "activity": "boxing", "sport": "boxing", "session_type": "pads",
                       "status": "done", "order": 2}]
    assert boxing["id"] not in json.dumps(fake.payloads[0])
    assert read_items(client) == []
    assert store.training_focus_inferences[0]["status"] == "none"


def test_general_pattern_feedback_is_kept_only_with_grounded_patterns(api):
    client, store, fake = api
    fake.answer = {"focus": "Brace your trunk before every rep under load.", "scope": "general_quality",
                   "category": "strength", "sport": None, "exercise_targets": [],
                   "movement_patterns": ["unilateral_squat", "hinge"]}
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Lost my brace on every loaded lift today.")
    [item] = read_items(client)
    assert item["scope"] == "general_quality" and item["movement_patterns"] == ["unilateral_squat", "hinge"]


@pytest.mark.parametrize("answer", [
    # A pattern the day never trained.
    {**SPLIT_SQUAT, "scope": "movement_pattern", "exercise_targets": [], "movement_patterns": ["vertical_push"]},
    # General quality without saying where it applies.
    {**SPLIT_SQUAT, "scope": "general_quality", "exercise_targets": [], "movement_patterns": []},
    # A category the day does not hold.
    {**SPLIT_SQUAT, "category": "conditioning"},
    # Workload, not technique.
    {**SPLIT_SQUAT, "focus": "Add weight to the split squat next time."},
    {**SPLIT_SQUAT, "focus": "Do 4 sets of split squats."},
    # Health language in the answer.
    {**SPLIT_SQUAT, "focus": "Protect the sore knee on split squats."},
    # An exercise the day lacks.
    {**SPLIT_SQUAT, "exercise_targets": ["push up"]},
])
def test_ungrounded_or_unsafe_answers_produce_no_suggestion(api, answer):
    client, store, fake = api
    fake.answer = answer
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert len(fake.payloads) == 1
    assert read_items(client) == []


def test_generated_session_types_ground_their_categories(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store, session_type="strength_power")
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert fake.payloads[0]["context"]["categories"] == ["strength"]
    assert len(read_items(client)) == 1


def test_ambiguous_note_produces_no_suggestion(api):
    client, store, fake = api
    fake.answer = {"focus": None, "scope": None, "category": None, "sport": None,
                   "exercise_targets": [], "movement_patterns": []}
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Good session.")
    assert read_items(client) == []
    assert store.training_focus_inferences[0]["status"] == "none"


# -- Manual takeaway and sparring logs -----------------------------------------


def test_manual_takeaway_uses_the_logged_exercises(api):
    client, store, fake = api
    fake.answer = {"focus": "Keep your hips square on each lunge.", "scope": "specific_exercise",
                   "category": "strength", "sport": None, "exercise_targets": ["walking lunge"], "movement_patterns": []}
    session = manual_session(client)
    put = client.put(f"/api/training/sessions/{session['id']}/exercises/{uuid4()}", headers=AUTH,
                     json={"exercise": {"name": "Walking Lunge"}, "format": "strength", "actual": {"sets": 3, "reps": 10}})
    assert put.status_code in {200, 201}, put.text
    save_takeaway(client, session["id"], "Hips kept twisting on the lunges.")
    noted = fake.payloads[0]["context"]["noted_session"]
    assert noted["activity"] == "strength"
    assert noted["exercises"][0]["name"] == "Walking Lunge" and noted["exercises"][0]["pattern"] == "unilateral_squat"
    [item] = read_items(client)
    assert item["exercise_targets"] == ["walking lunge"]


def test_sparring_notes_give_a_sport_technique_focus(api):
    client, store, fake = api
    fake.answer = {"focus": "Return the jab straight to guard after throwing.", "scope": "sport_technique",
                   "category": "sparring", "sport": "boxing", "exercise_targets": [], "movement_patterns": []}
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    sparring_detail(client, session["id"], notes="Kept getting countered after my jab, hand came back low.")
    payload = fake.payloads[0]
    assert payload["source"] == "sparring_log" and payload["context"]["sports"] == ["boxing"]
    assert payload["context"]["noted_session"]["sparring"] == {"intensity": "medium", "rounds": 4}
    [item] = read_items(client)
    assert item["scope"] == "sport_technique" and item["sport"] == "boxing"


def test_explicit_sparring_focus_is_never_inferred_over(api):
    client, store, fake = api
    fake.answer = {"focus": "Keep the rear hand home.", "scope": "sport_technique", "category": "sparring",
                   "sport": None, "exercise_targets": [], "movement_patterns": []}
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    sparring_detail(client, session["id"], notes="Got caught with the right hand.", focus="Slip outside the jab")
    # The takeaway on the same session yields to the athlete's own focus.
    save_takeaway(client, session["id"], "Rear hand kept dropping.")
    assert fake.payloads == []
    items = read_items(client)
    assert [(item["origin"], item["focus"]) for item in items] == [("athlete", "Slip outside the jab")]


def test_explicit_focus_added_later_hides_the_inference_from_that_session(api):
    client, store, fake = api
    fake.answer = {"focus": "Keep the rear hand home.", "scope": "sport_technique", "category": "sparring",
                   "sport": "boxing", "exercise_targets": [], "movement_patterns": []}
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    sparring_detail(client, session["id"], notes="Got caught with the right hand.")
    assert [item["origin"] for item in read_items(client)] == ["inferred"]
    sparring_detail(client, session["id"], notes="Got caught with the right hand.", focus="Slip outside the jab",
                    detail_revision=2)
    assert [item["origin"] for item in read_items(client)] == ["athlete"]


def test_rocked_or_health_notes_never_reach_the_model(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    sparring_detail(client, session["id"], notes="Felt dizzy after round three.")
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Knee pain on split squats.")
    assert fake.payloads == []
    assert read_items(client) == []


# -- Revisions, duplicates, stale results, deletion ----------------------------


def test_same_note_is_never_generated_twice_and_an_edit_replaces_it(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    client.patch(f"/api/training/history/unlxck/{row['id']}", headers=AUTH, json={"session_rpe": 8})
    assert len(fake.payloads) == 1
    fake.answer = {**SPLIT_SQUAT, "focus": "Drive through the front foot on RDLs.", "exercise_targets": ["romanian deadlift"]}
    edit_notes(client, row["id"], "RDL bar drifted away from my legs.")
    assert len(fake.payloads) == 2
    [item] = read_items(client)
    assert item["exercise_targets"] == ["romanian deadlift"]
    assert len(store.training_focus_inferences) == 1


def test_a_note_edited_mid_call_is_not_overwritten_by_the_older_result(api):
    client, store, fake = api
    row = lower_body_completion(store)
    newer = {**SPLIT_SQUAT, "focus": "Keep the bar close on every RDL.", "exercise_targets": ["romanian deadlift"]}

    def athlete_edits_while_waiting():
        fake.answer = newer
        edit_notes(client, row["id"], "RDL bar drifted away from my legs.")
        fake.answer = SPLIT_SQUAT  # the slow, older call finishes with this

    fake.during = athlete_edits_while_waiting
    fake.answer = SPLIT_SQUAT
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    [item] = read_items(client)
    assert item["focus"] == "Keep the bar close on every RDL."


def test_inference_disappears_when_the_note_changes_without_a_new_result(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    store.update_session_completion_fields(ATHLETE_ID, row["id"], {"notes": "Changed elsewhere"})
    assert read_items(client) == []


def test_deleted_sessions_take_their_inference_with_them(api):
    client, store, fake = api
    fake.answer = {"focus": "Keep your hips square on each lunge.", "scope": "movement_pattern",
                   "category": "strength", "sport": None, "exercise_targets": [], "movement_patterns": ["unilateral_squat"]}
    session = manual_session(client)
    client.put(f"/api/training/sessions/{session['id']}/exercises/{uuid4()}", headers=AUTH,
               json={"exercise": {"name": "Reverse Lunge"}, "format": "strength", "actual": {"sets": 3, "reps": 8}})
    save_takeaway(client, session["id"], "Hips twisted on lunges.")
    assert len(read_items(client)) == 1
    assert client.delete(f"/api/training/sessions/{session['id']}", headers=AUTH).status_code == 204
    assert read_items(client) == []
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert len(read_items(client)) == 1
    assert client.delete(f"/api/training/history/unlxck/{row['id']}", headers=AUTH).status_code == 204
    assert read_items(client) == []


def test_dismissed_suggestion_stays_hidden(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    [item] = read_items(client)
    assert client.delete(f"/api/today/training-focus/{item['id']}", headers=AUTH).status_code == 204
    assert read_items(client) == []
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert read_items(client) == [] and len(fake.payloads) == 1


def test_stale_inference_is_not_shown(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    from datetime import date, timedelta
    old_day = (date.fromisoformat(today()) - timedelta(days=28)).isoformat()
    row = lower_body_completion(store, day=old_day)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert len(fake.payloads) == 1 and read_items(client) == []


# -- Failure, consent, configuration --------------------------------------------


def test_model_failure_never_blocks_the_save(api):
    client, store, fake = api
    fake.error = RuntimeError("provider down")
    row = lower_body_completion(store)
    response = edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert response.json()["notes"] == "Balance on split squats was poor."
    assert store.training_focus_inferences[0]["status"] == "failed"
    assert read_items(client) == []


def test_store_failure_in_inference_never_blocks_the_save(api, monkeypatch):
    client, store, fake = api
    monkeypatch.setattr(store, "claim_training_focus_inference",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    row = lower_body_completion(store)
    assert edit_notes(client, row["id"], "Balance on split squats was poor.").status_code == 200


def test_no_inference_or_reminder_without_health_consent(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert len(read_items(client)) == 1
    withdraw_health_consent(store)
    assert read_items(client) == []
    edit_notes(client, row["id"], "Balance on RDLs was poor.")
    assert len(fake.payloads) == 1


def test_disabled_without_an_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(inference, "_inferrer_cache", {})
    client, store, _ = _build_client()
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert store.training_focus_inferences == [] and read_items(client) == []


def test_daily_limit_bounds_model_calls(api, monkeypatch):
    client, store, fake = api
    monkeypatch.setenv("UNLXCK_FOCUS_DAILY_LIMIT", "1")
    fake.answer = SPLIT_SQUAT
    first = lower_body_completion(store, session_id="s-a")
    second = lower_body_completion(store, session_id="s-b")
    edit_notes(client, first["id"], "Balance on split squats was poor.")
    edit_notes(client, second["id"], "Balance on split squats was poor again.")
    assert len(fake.payloads) == 1
    assert store.training_focus_inferences[1]["reason"] == "daily_limit"


def test_openai_inferrer_uses_its_own_model_and_structured_output(monkeypatch):
    calls = []

    class Usage:
        input_tokens, output_tokens = 512, 41

    class Responses:
        def create(self, **request):
            calls.append(request)
            return type("R", (), {"output_text": json.dumps(SPLIT_SQUAT), "usage": Usage()})()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("UNLXCK_STAGE2_MODEL", "plan-model")
    monkeypatch.delenv("UNLXCK_FOCUS_MODEL", raising=False)
    monkeypatch.setattr(inference, "_inferrer_cache", {})
    inferrer = inference.get_focus_inferrer()
    assert inferrer.model == "gpt-6-luna"
    inferrer.client = type("C", (), {"responses": Responses()})()
    result = inferrer.infer({"note": "x", "context": {}})
    assert result.inferred.exercise_targets == ["split squat"] and result.input_tokens == 512
    [request] = calls
    assert request["model"] == "gpt-6-luna" and request["store"] is False
    assert request["text"]["format"] == response_schema() and request["max_output_tokens"] == 400
    assert "reasoning" not in request
    monkeypatch.setenv("UNLXCK_FOCUS_MODEL", "focus-model-x")
    monkeypatch.setattr(inference, "_inferrer_cache", {})
    assert inference.get_focus_inferrer().model == "focus-model-x"


def test_movement_patterns_match_the_shared_web_fixture():
    cases = json.loads((ROOT / "web" / "test" / "fixtures" / "training-focus-patterns.json").read_text())
    for name, pattern in cases.items():
        assert movement_pattern(name) == pattern, name


# -- Regressions: grounding in what was actually performed ----------------------


@pytest.mark.parametrize("logs, completion_status", [
    # The split squat itself was skipped.
    ([("b1", "skipped", {}), ("b2", "as_prescribed", {"sets": 3, "reps": 6})], "modified"),
    # Other blocks were logged but the split squat was not: no record it was done.
    ([("b2", "as_prescribed", {"sets": 3, "reps": 6})], "done"),
    # A modified session with no per-exercise record: blocks may have been replaced.
    ([], "modified"),
])
def test_cues_are_never_grounded_in_skipped_or_unverified_work(api, logs, completion_status):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store, exercise_logs=logs)
    store.update_session_completion_fields(ATHLETE_ID, row["id"], {})
    for stored in store.session_completions[ATHLETE_ID]:
        if stored["id"] == row["id"]:
            stored["status"] = completion_status
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    split = fake.payloads[0]["context"]["noted_session"]["exercises"][0]
    assert split["name"] == "Split Squat" and split["status"] in {"skipped", "unverified"}
    assert read_items(client) == []
    assert store.training_focus_inferences[0]["reason"] == "no_suggestion"


def test_a_session_completed_as_prescribed_without_results_counts_as_performed(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    statuses = [item["status"] for item in fake.payloads[0]["context"]["noted_session"]["exercises"]]
    assert statuses == ["done", "done", "done"]
    assert len(read_items(client)) == 1


# -- Regressions: notes that point at another session that day -----------------


def test_a_sparring_note_can_point_at_an_earlier_session_that_day(api):
    client, store, fake = api
    lower_body_completion(store, exercise_logs=[("b1", "skipped", {}), ("b2", "as_prescribed", {"sets": 3, "reps": 6})])
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    fake.answer = {"focus": "Keep the bar close through every RDL.", "scope": "specific_exercise", "category": "strength",
                   "sport": None, "exercise_targets": ["romanian deadlift"], "movement_patterns": []}
    sparring_detail(client, session["id"], notes="Legs were gone from the RDLs this morning, bar drifted forward.")
    context = fake.payloads[0]["context"]
    [earlier] = context["other_sessions_same_day"]
    assert earlier["order"] == 1 and context["noted_session"]["order"] == 2
    assert [(item["name"], item["status"]) for item in earlier["exercises"]] == [
        ("Split Squat", "skipped"), ("Romanian Deadlift (RDL)", "done"), ("Calf Raises", "unverified")]
    assert "prescribed" not in earlier["exercises"][1]  # other sessions stay compact
    assert set(context["categories"]) >= {"strength", "sparring"}
    [item] = read_items(client)
    assert item["exercise_targets"] == ["romanian deadlift"]


def test_an_earlier_sessions_skipped_exercise_cannot_be_targeted(api):
    client, store, fake = api
    lower_body_completion(store, exercise_logs=[("b1", "skipped", {}), ("b2", "as_prescribed", {"sets": 3, "reps": 6})])
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    fake.answer = SPLIT_SQUAT
    sparring_detail(client, session["id"], notes="Split squats this morning were shaky.")
    assert read_items(client) == []


# -- Regressions: exercise context changes after the cue ------------------------


def lunge_cue(client, fake):
    fake.answer = {"focus": "Keep your hips square on each lunge.", "scope": "specific_exercise",
                   "category": "strength", "sport": None, "exercise_targets": ["walking lunge"], "movement_patterns": []}
    session = manual_session(client)
    lunge = str(uuid4())
    client.put(f"/api/training/sessions/{session['id']}/exercises/{lunge}", headers=AUTH,
               json={"exercise": {"name": "Walking Lunge"}, "format": "strength", "actual": {"sets": 3, "reps": 10}})
    save_takeaway(client, session["id"], "Hips kept twisting on the lunges.")
    assert len(read_items(client)) == 1
    return session, lunge


def test_removing_the_targeted_exercise_withdraws_the_cue_without_a_model_call(api):
    client, store, fake = api
    session, lunge = lunge_cue(client, fake)
    assert client.delete(f"/api/training/sessions/{session['id']}/exercises/{lunge}", headers=AUTH).status_code == 204
    assert read_items(client) == []
    assert store.training_focus_inferences[0]["reason"] == "context_changed"
    assert len(fake.payloads) == 1
    # The note is unchanged, so the same takeaway is not re-sent either.
    save_takeaway(client, session["id"], "Hips kept twisting on the lunges.")
    assert len(fake.payloads) == 1


def test_an_unrelated_context_change_keeps_and_restamps_the_cue(api):
    client, store, fake = api
    session, _ = lunge_cue(client, fake)
    before = store.training_focus_inferences[0]["context_revision"]
    client.put(f"/api/training/sessions/{session['id']}/exercises/{uuid4()}", headers=AUTH,
               json={"exercise": {"name": "Calf Raises"}, "format": "strength", "actual": {"sets": 2, "reps": 15}})
    [item] = read_items(client)
    assert item["exercise_targets"] == ["walking lunge"]
    assert store.training_focus_inferences[0]["context_revision"] not in {before, None}
    assert len(fake.payloads) == 1


def test_a_plan_exercise_skipped_after_the_note_withdraws_the_cue(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert len(read_items(client)) == 1
    store.exercise_logs.append({"id": str(uuid4()), "athlete_id": ATHLETE_ID, "plan_id": row["plan_id"],
                                "training_day": row["training_day"], "session_id": "s-lower", "block_id": "b1",
                                "status": "skipped", "actual": {}, "created_at": f"{row['training_day']}T11:00:00+00:00"})
    assert inference.refresh_day_focus(store, ATHLETE_ID, row["training_day"]) == 1
    assert read_items(client) == []


def test_record_changed_while_the_model_answers_is_grounded_on_the_new_record(api):
    client, store, fake = api
    session = manual_session(client)
    lunge = str(uuid4())
    client.put(f"/api/training/sessions/{session['id']}/exercises/{lunge}", headers=AUTH,
               json={"exercise": {"name": "Walking Lunge"}, "format": "strength", "actual": {"sets": 3, "reps": 10}})
    fake.answer = {"focus": "Keep your hips square on each lunge.", "scope": "specific_exercise",
                   "category": "strength", "sport": None, "exercise_targets": ["walking lunge"], "movement_patterns": []}
    fake.during = lambda: store.delete_manual_exercise(ATHLETE_ID, lunge)
    save_takeaway(client, session["id"], "Hips kept twisting on the lunges.")
    assert read_items(client) == []


def test_exercise_log_writes_schedule_a_refresh(api, monkeypatch):
    client, store, fake = api
    calls = []
    monkeypatch.setattr(inference, "refresh_day_focus", lambda store, athlete_id, day, **kw: calls.append(day))
    monkeypatch.setattr(today_routes, "record_exercise_log", lambda *a, **k: {
        "id": str(uuid4()), "athlete_id": ATHLETE_ID, "plan_id": str(uuid4()), "session_id": "s", "block_id": "b1",
        "training_day": "2026-11-01", "status": "skipped", "prescribed": {}, "actual": {}, "created_at": "x", "updated_at": "x"})
    monkeypatch.setattr(today_routes, "reconcile_training_xp", lambda *a, **k: None)
    response = client.post("/api/today/exercise-log", headers=AUTH,
                           json={"plan_id": str(uuid4()), "block_id": "b1", "status": "skipped"})
    assert response.status_code == 201, response.text
    assert calls == ["2026-11-01"]


# -- Regressions: candidate window and call cap ---------------------------------


def test_every_recent_candidate_is_returned_for_matching(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    for index in range(8):
        row = lower_body_completion(store, session_id=f"s-{index}")
        edit_notes(client, row["id"], f"Balance on split squats was poor, take {index}.")
    assert len(read_items(client)) == 8


def test_daily_cap_counts_every_call_not_rows(api, monkeypatch):
    client, store, fake = api
    monkeypatch.setenv("UNLXCK_FOCUS_DAILY_LIMIT", "2")
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    edit_notes(client, row["id"], "Balance on split squats was still poor.")
    edit_notes(client, row["id"], "Balance on split squats was poor again.")
    # One stored row, three edits: the third call is refused.
    assert len(fake.payloads) == 2 and len(store.training_focus_inferences) == 1
    assert store.training_focus_inferences[0]["reason"] == "daily_limit"


def test_failed_calls_count_towards_the_cap(api, monkeypatch):
    client, store, fake = api
    monkeypatch.setenv("UNLXCK_FOCUS_DAILY_LIMIT", "1")
    fake.error = RuntimeError("provider down")
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    fake.error = None
    fake.answer = SPLIT_SQUAT
    edit_notes(client, row["id"], "Balance on split squats was poor again.")
    assert len(fake.payloads) == 1


# -- Regressions: schema and disclosure ------------------------------------------


def test_migration_schema_and_requirements_agree():
    from api.schema_requirements import REQUIRED_COLUMNS as REQUIRED_TABLE_COLUMNS, REQUIRED_FUNCTIONS
    migration = (ROOT / "supabase" / "migrations" / "20261102215720_add_training_focus_inferences.sql").read_text()
    schema = (ROOT / "supabase" / "schema.sql").read_text()
    for table in ("training_focus_inferences", "training_focus_call_budget"):
        for column in REQUIRED_TABLE_COLUMNS[table]:
            assert column in migration and column in schema, (table, column)
    for function in ("public.claim_training_focus_inference", "public.reserve_training_focus_call"):
        assert function in REQUIRED_FUNCTIONS
        assert f"create or replace function {function}" in migration
        assert f"create or replace function {function}" in schema


def test_privacy_notice_discloses_training_note_processing():
    notice = (ROOT / "docs" / "privacy-notice.md").read_text()
    assert "training-focus suggestions" in notice and "no screen catches every wording" in notice
    assert "first 600 characters" in notice


# -- Regressions: head impact never reaches the model --------------------------


@pytest.mark.parametrize("note", [
    "Took a lot of head contact in round two.",
    "Ate too many head shots, need to keep my hands up.",
    "Got clipped late and kept going.",
    "He caught me on the chin twice with the overhand.",
    "Too many shots to the head off the jab.",
    "Was wobbled in the third, keep my guard higher.",
    "Saw stars after the right hand.",
])
def test_head_impact_notes_are_blocked_before_any_model_call(api, note):
    client, store, fake = api
    fake.answer = {"focus": "Keep your guard high after the jab.", "scope": "sport_technique", "category": "sparring",
                   "sport": "boxing", "exercise_targets": [], "movement_patterns": []}
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    sparring_detail(client, session["id"], notes=note)
    save_takeaway(client, session["id"], note)
    row = lower_body_completion(store)
    edit_notes(client, row["id"], note)
    assert fake.payloads == []
    assert read_items(client) == []


def test_heavy_head_contact_report_blocks_the_note_whatever_it_says(api):
    client, store, fake = api
    fake.answer = {"focus": "Keep your guard high after the jab.", "scope": "sport_technique", "category": "sparring",
                   "sport": "boxing", "exercise_targets": [], "movement_patterns": []}
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    sparring_detail(client, session["id"], notes="Guard dropped after the jab.", head_contact="heavy")
    assert fake.payloads == []


def test_technique_notes_that_only_sound_alike_still_work(api):
    client, store, fake = api
    fake.answer = {"focus": "Return the jab straight to guard.", "scope": "sport_technique", "category": "sparring",
                   "sport": "boxing", "exercise_targets": [], "movement_patterns": []}
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    sparring_detail(client, session["id"], notes="Kept dropping my right hand after the jab; chin was tucked.")
    assert len(fake.payloads) == 1 and len(read_items(client)) == 1


# -- Regressions: logged performance of the cue's exercise ----------------------


def put_lunges(client, session_id, entry_id, *, reps, revision, name="Walking Lunge"):
    response = client.put(f"/api/training/sessions/{session_id}/exercises/{entry_id}", headers=AUTH, json={
        "exercise": {"name": name}, "format": "strength", "actual": {"sets": 3, "reps": reps}, "revision": revision})
    assert response.status_code in {200, 201}, response.text


def test_changed_results_for_the_targeted_exercise_withdraw_the_cue(api):
    client, store, fake = api
    session, lunge = lunge_cue(client, fake)
    put_lunges(client, session["id"], lunge, reps=4, revision=1)
    assert read_items(client) == []
    assert store.training_focus_inferences[0]["reason"] == "context_changed"
    assert len(fake.payloads) == 1


def test_changed_results_for_another_exercise_keep_the_cue(api):
    client, store, fake = api
    session, _ = lunge_cue(client, fake)
    calf = str(uuid4())
    put_lunges(client, session["id"], calf, reps=15, revision=0, name="Calf Raises")
    put_lunges(client, session["id"], calf, reps=20, revision=1, name="Calf Raises")
    assert [item["exercise_targets"] for item in read_items(client)] == [["walking lunge"]]


def test_changed_plan_results_for_the_targeted_exercise_withdraw_the_cue(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store, exercise_logs=[("b1", "as_prescribed", {"sets": 3, "reps": 8})])
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert len(read_items(client)) == 1
    next(log for log in store.exercise_logs if log["block_id"] == "b1").update(status="modified", actual={"sets": 1, "reps": 8})
    assert inference.refresh_day_focus(store, ATHLETE_ID, row["training_day"]) == 1
    assert read_items(client) == []


# -- Regressions: a technique cue keeps the sport it was recorded for ------------


TECHNIQUE = {"focus": "Return the jab straight to guard.", "scope": "sport_technique", "category": "sparring",
             "sport": None, "exercise_targets": [], "movement_patterns": []}


def timer_sparring(client, store, *, plan_sport=None, **fields):
    from tests.test_api_sparring_log import PLAN_ID, _body, _seed_plan
    _seed_plan(store)
    if plan_sport:
        store.plans[PLAN_ID]["structured_plan"] = {"plan_metadata": {"sport": plan_sport}}
    response = client.post("/api/today/sparring-log", headers=AUTH, json=_body(
        **{"notes": "Hand came back low after every jab.", "head_contact": "light", **fields}))
    assert response.status_code == 201, response.text


def test_timer_note_takes_the_athletes_single_sport(api):
    client, store, fake = api
    store.profiles[ATHLETE_ID]["technical_style"] = ["Boxing"]
    fake.answer = TECHNIQUE
    timer_sparring(client, store)
    assert fake.payloads[0]["context"]["sports"] == ["boxing"]
    [item] = read_items(client)
    assert item["sport"] == "boxing"


def test_plan_sport_wins_over_the_athletes_styles(api):
    client, store, fake = api
    store.profiles[ATHLETE_ID]["technical_style"] = ["boxing", "muay_thai"]
    fake.answer = TECHNIQUE
    timer_sparring(client, store, plan_sport="Muay Thai")
    [item] = read_items(client)
    assert item["sport"] == "muay_thai"


def test_technique_cue_without_a_known_sport_is_not_kept(api):
    client, store, fake = api
    store.profiles[ATHLETE_ID]["technical_style"] = ["boxing", "muay_thai"]
    fake.answer = TECHNIQUE
    timer_sparring(client, store)
    assert len(fake.payloads) == 1
    assert read_items(client) == []
    assert store.training_focus_inferences[0]["reason"] == "no_suggestion"


def test_technique_cue_cannot_claim_a_different_sport(api):
    client, store, fake = api
    fake.answer = {**TECHNIQUE, "sport": "muay_thai"}
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    sparring_detail(client, session["id"], notes="Hand came back low after every jab.")
    assert read_items(client) == []


# -- Follow-up review fixes -----------------------------------------------------


def test_consent_withdrawn_before_the_background_run_stops_the_note(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store, notes="Balance on split squats was poor.")
    withdraw_health_consent(store)
    assert inference.run_focus_inference(store, ATHLETE_ID, "plan_completion", row["id"], inferrer=fake) == "no_consent"
    assert fake.payloads == [] and store.training_focus_inferences == []


def test_warm_up_and_mindset_blocks_are_not_grounding_exercises(api):
    client, store, fake = api
    row = lower_body_completion(store)
    snapshot = next(r for r in store.session_completions[ATHLETE_ID] if r["id"] == row["id"])["prescription_snapshot"]
    snapshot["session"]["blocks"] = [
        {"block_id": "w1", "block_type": "warmup", "display_name": "Bodyweight Split Squat"},
        {"block_id": "m1", "block_type": "mindset", "display_name": "Fight Visualisation"},
        {"block_id": "b2", "block_type": "strength", "display_name": "Romanian Deadlift (RDL)", "sets": 3, "reps": "6"},
    ]
    fake.answer = {**SPLIT_SQUAT, "exercise_targets": ["bodyweight split squat"]}
    edit_notes(client, row["id"], "Balance on the split squats in the warm-up was poor.")
    assert [item["name"] for item in fake.payloads[0]["context"]["noted_session"]["exercises"]] == ["Romanian Deadlift (RDL)"]
    assert read_items(client) == []


@pytest.mark.parametrize("focus", ["Use less rest between rounds.", "Take a faster pace on intervals.",
                                   "Push harder on every split squat."])
def test_rest_pace_and_intensity_changes_are_rejected(api, focus):
    client, store, fake = api
    fake.answer = {**SPLIT_SQUAT, "focus": focus}
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert read_items(client) == []


def test_a_specific_exercise_cue_names_exactly_one_exercise(api):
    client, store, fake = api
    fake.answer = {**SPLIT_SQUAT, "exercise_targets": ["split squat", "calf raises"]}
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert read_items(client) == []


def test_a_sport_change_on_the_noted_session_withdraws_the_technique_cue(api):
    client, store, fake = api
    fake.answer = {"focus": "Return the jab straight to guard.", "scope": "sport_technique", "category": "sparring",
                   "sport": "boxing", "exercise_targets": [], "movement_patterns": []}
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    sparring_detail(client, session["id"], notes="Hand came back low after every jab.")
    assert len(read_items(client)) == 1
    edited = client.patch(f"/api/training/sessions/{session['id']}", headers=AUTH, json={
        "activity": "sparring", "sport": "muay_thai", "session_type": "sparring", "duration_minutes": 45, "effort": "hard"})
    assert edited.status_code == 200, edited.text
    assert read_items(client) == []


@pytest.mark.parametrize("note", ["Ringing the bell at the end of each round helped my pacing.",
                                  "Memory drill for combos went well, but my jab dropped after the double."])
def test_ordinary_words_near_head_impact_terms_still_reach_the_model(api, note):
    client, store, fake = api
    fake.answer = None
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    sparring_detail(client, session["id"], notes=note)
    assert [payload["note"] for payload in fake.payloads] == [note]


@pytest.mark.parametrize("note", ["Ears ringing after the session.", "Some memory loss after round two.",
                                  "My head was ringing after that jab.", "Lost my memory for a second in round three.",
                                  "Short memory lapse after the right hand."])
def test_head_impact_symptom_wording_is_screened(note):
    from api.contracts.training_focus import mentions_health
    assert mentions_health(note)


@pytest.mark.parametrize("focus", ["Do more rounds on the bag.", "Use less duration per interval.",
                                   "Add more duration to each hold."])
def test_more_or_less_duration_and_rounds_are_workload(focus):
    from api.contracts.training_focus import _WORKLOAD
    assert _WORKLOAD.search(focus)


def test_malformed_plan_metadata_does_not_break_inference():
    class Store:
        def get_plan_for_athlete(self, plan_id, athlete_id):
            return {"structured_plan": {"plan_metadata": "not-a-dict"}}
    assert inference._plan_sport(Store(), ATHLETE_ID, "plan-1") == set()


def test_focus_read_skips_the_sparring_window_without_sparring_inferences(api, monkeypatch):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    windows = []
    real = store.list_sparring_logs
    monkeypatch.setattr(store, "list_sparring_logs", lambda athlete_id, **kw: windows.append(kw) or real(athlete_id, **kw))
    assert len(read_items(client)) == 1
    assert all(kw.get("with_focus") for kw in windows)


def test_warm_up_mobility_blocks_add_no_category(api):
    client, store, fake = api
    row = lower_body_completion(store)
    snapshot = next(r for r in store.session_completions[ATHLETE_ID] if r["id"] == row["id"])["prescription_snapshot"]
    snapshot["session"]["blocks"].insert(0, {"block_id": "w1", "block_type": "mobility_activation",
                                             "display_name": "Hip Openers"})
    snapshot["session"]["blocks"].append({"block_id": "c1", "block_type": "cooldown_recovery", "display_name": "Breathing"})
    fake.answer = {**SPLIT_SQUAT, "category": "mobility"}
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert fake.payloads[0]["context"]["categories"] == ["strength"]
    assert read_items(client) == []


def test_consent_withdrawn_while_context_is_read_stops_the_send(api, monkeypatch):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store, notes="Balance on split squats was poor.")
    real = inference.build_context

    def withdraw_then_build(*args, **kwargs):
        withdraw_health_consent(store)
        return real(*args, **kwargs)

    monkeypatch.setattr(inference, "build_context", withdraw_then_build)
    assert inference.run_focus_inference(store, ATHLETE_ID, "plan_completion", row["id"], inferrer=fake) == "no_consent"
    assert fake.payloads == []


def test_refresh_reads_nothing_without_consent(api, monkeypatch):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    withdraw_health_consent(store)
    monkeypatch.setattr(inference, "build_context", lambda *a, **k: pytest.fail("read without consent"))
    assert inference.refresh_day_focus(store, ATHLETE_ID, row["training_day"]) == 0


def test_explicit_sparring_focus_does_not_trigger_the_sparring_window_read(api, monkeypatch):
    client, store, fake = api
    fake.answer = {"focus": "Keep the rear hand home.", "scope": "sport_technique", "category": "sparring",
                   "sport": "boxing", "exercise_targets": [], "movement_patterns": []}
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    log = sparring_detail(client, session["id"], notes="Got caught with the right hand.")
    sparring_detail(client, session["id"], notes="Got caught with the right hand.", focus="Slip outside the jab",
                    detail_revision=2)
    assert any(row["source_id"] == log["id"] for row in store.training_focus_inferences)
    windows = []
    real = store.list_sparring_logs
    monkeypatch.setattr(store, "list_sparring_logs", lambda athlete_id, **kw: windows.append(kw) or real(athlete_id, **kw))
    assert [item["origin"] for item in read_items(client)] == ["athlete"]
    assert all(kw.get("with_focus") for kw in windows)
