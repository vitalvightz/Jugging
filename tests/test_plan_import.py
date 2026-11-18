"""Coach programme import on the Custom Build model.

What the model reads becomes an ordinary ``CustomPlanInput``: the same schema,
validation, builder and save path as a plan built by hand. These tests hold the
import to three promises:

* schema compatibility: every draft validates as ``CustomPlanInput`` and saves
  through the existing endpoints unchanged;
* faithful extraction: the coach's names, doses and instructions arrive exactly,
  nothing is invented, and whatever can't be carried exactly is flagged;
* save/reload integrity: a saved import reads back identical, for athletes and
  for coaches.
"""
import base64
import json
import re
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from api.contracts.custom_plans import WEEKDAYS, CustomPlanInput
from api.services import plan_import
from api.services.coach_plans import saved_input
from api.services.plan_import import (
    PlanImportRequest, ReadExercise, ReadPlan, ReadSession, ReadWeek, build_draft, dose_from, response_schema,
)
from tests.support import _build_client
from tests.test_coach_fighter_access import AS_COACH, BASE
from tests.test_coach_fighter_access import camp  # noqa: F401 - shared pytest fixture
from tests.test_coach_plans import plans  # noqa: F401 - shared pytest fixture

AUTH = {"Authorization": "Bearer athlete-token"}
URL = "/api/custom-plans/import"
MONDAY = "2026-11-23"
FIXTURES = Path(__file__).parent / "fixtures" / "plan_import"
PROGRAMME = (FIXTURES / "coach_programme.txt").read_text()
READING = ReadPlan.model_validate(json.loads((FIXTURES / "coach_programme.model.json").read_text()))


class FakeReader:
    def __init__(self, plan: ReadPlan | Exception):
        self.plan, self.texts, self.images = plan, [], []

    def read(self, text, images=()):
        self.texts.append(text)
        self.images = list(images)
        if isinstance(self.plan, Exception):
            raise self.plan
        return self.plan


@pytest.fixture
def api(monkeypatch):
    client, store, _ = _build_client()
    plan_import.reset_import_limits()
    yield client, store, monkeypatch
    plan_import.reset_import_limits()
    plan_import._reader_cache.clear()


def use(monkeypatch, reader):
    monkeypatch.setitem(plan_import._reader_cache, "default", reader)


def dose(exercise):
    return {key: value for key, value in exercise["dose"].items() if value is not None}


def place(body, weekday):
    """What the app does when the person picks a day for each session the coach left without one."""
    plan = json.loads(json.dumps(body["plan"]))
    waiting = {sid for group in body["needs_day"] for sid in group["session_ids"]}
    for session in plan["sessions"]:
        if session["id"] in waiting:
            if session["scheduled_date"]:
                monday = date.fromisoformat(session["scheduled_date"])
                session["scheduled_date"] = (monday + timedelta(days=WEEKDAYS.index(weekday) - monday.weekday())).isoformat()
            session["weekday"] = weekday
    return plan


def run_import(client, monkeypatch, reading=READING, text=PROGRAMME):
    use(monkeypatch, FakeReader(reading))
    response = client.post(URL, json={"text": text, "file_name": "camp.docx", "start_date": MONDAY}, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


# -- Schema compatibility --------------------------------------------------------

def test_the_model_schema_is_strict_and_every_draft_is_a_custom_plan_input():
    schema = response_schema()
    assert schema["strict"] is True

    def check(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                check(child)
        if node.get("type") == "array":
            check(node["items"])
    check(schema["schema"])
    assert PlanImportRequest.model_fields["text"].metadata

    # Awkward readings still produce a valid Custom Build plan, never an error.
    odd = ReadPlan(name="x" * 300, weeks=[ReadWeek(repeat=0, sessions=[
        ReadSession(day="Funday", name="", session_type="boxing", exercises=[
            ReadExercise(name="  ", sets=3), ReadExercise(name="y" * 150, sets=0, reps=-2, rpe=11, rest_seconds=9999,
                                                       load="z" * 120, equipment="e" * 200, cues=["c" * 600, "ok"]),
        ])])])
    for start in (date(2026, 11, 23), date(2026, 11, 26)):
        result = build_draft(odd, start)
        CustomPlanInput.model_validate(result.plan)
    exercise = result.plan["sessions"][0]["exercises"][0]
    assert exercise["name"] == "y" * 100 and exercise["coaching_cues"] == ["ok"]
    assert exercise["notes"].splitlines() == [f"Full name: {'y' * 150}", f"Load: {'z' * 120}", f"Equipment: {'e' * 200}", "RPE as written: 11", "Rest as written: 9999s", f"Cue: {'c' * 600}"]


def test_the_draft_saves_through_the_existing_endpoint_and_reads_back_identical(api):
    client, _, monkeypatch = api
    body = run_import(client, monkeypatch)
    draft = place(body, "Friday")
    # Nothing is stored by the import itself.
    assert client.get("/api/custom-plans", headers=AUTH).json() == []
    saved = client.post("/api/custom-plans", json=draft, headers=AUTH)
    assert saved.status_code == 201, saved.text
    reloaded = client.get(f"/api/custom-plans/{saved.json()['id']}", headers=AUTH).json()
    assert saved_input(reloaded).model_dump(mode="json") == CustomPlanInput.model_validate(draft).model_dump(mode="json")
    # Saving the reloaded plan again changes nothing.
    again = client.put(f"/api/custom-plans/{reloaded['id']}", json=saved_input(reloaded).model_dump(mode="json"), headers=AUTH)
    assert again.status_code == 200, again.text
    assert saved_input(again.json()).model_dump(mode="json") == saved_input(reloaded).model_dump(mode="json")
    # Only rounds with no written time or rest stay marked for the person to finish.
    assert [e["name"] for s in reloaded["sessions"][:4] for e in s["exercises"] if e["incomplete"]] == [
        "Shadow boxing", "Hard sparring"]


def test_a_coach_import_saves_and_reloads_through_the_coach_plan_path(plans, monkeypatch):  # noqa: F811
    client, _, _ = plans
    plan_import.reset_import_limits()
    use(monkeypatch, FakeReader(READING))
    imported = client.post(URL, json={"text": PROGRAMME, "start_date": MONDAY}, headers=AS_COACH)
    assert imported.status_code == 200, imported.text
    draft = place(imported.json(), "Thursday")
    created = client.post(f"{BASE}/plans?request_id={uuid4()}", headers=AS_COACH, json=draft)
    assert created.status_code == 201, created.text
    reloaded = client.get(f"{BASE}/plans/{created.json()['id']}", headers=AS_COACH).json()
    assert saved_input(reloaded).model_dump(mode="json") == CustomPlanInput.model_validate(draft).model_dump(mode="json")
    plan_import._reader_cache.clear()


# -- Faithful extraction -----------------------------------------------------------

def test_the_coach_programme_arrives_exactly_as_written(api):
    client, _, monkeypatch = api
    body = run_import(client, monkeypatch)
    plan = body["plan"]
    assert (body["weeks"], body["sessions"], body["exercises"]) == (3, 12, 27)
    assert plan["name"] == "Fight Camp – Phase 1"
    assert (plan["schedule_mode"], plan["start_date"], plan["end_date"]) == ("dated", "2026-11-23", "2026-12-13")
    week = plan["sessions"][:4]
    # Written days are kept. "Day 3" has no weekday, so it isn't given one: the
    # person picks it before the draft opens (it waits on Monday until then).
    assert [(s["name"], s["scheduled_date"], s["session_type"]) for s in week] == [
        ("Strength", "2026-11-23", "strength_power"), ("Day 3 - Roadwork", "2026-11-23", "conditioning"),
        ("Pads", "2026-11-25", "skill"), ("Sparring", "2026-11-28", "sparring")]
    road_ids = [s["id"] for s in plan["sessions"] if s["name"] == "Day 3 - Roadwork"]
    assert body["needs_day"] == [{"where": "Weeks 1-3 · Day 3 - Roadwork", "session_ids": road_ids}] and len(road_ids) == 3
    # The coach's session note is kept on the session itself, word for word.
    assert week[0]["notes"] == "Focus: brace hard, own every rep."
    assert [s["notes"] for s in week[1:]] == [None, None, None]
    week = [week[0], week[2], week[1], week[3]]
    strength, pads, road, sparring = (s["exercises"] for s in week)
    deadlift = strength[0]
    assert (deadlift["origin"], deadlift["name"]) == ("manual", "Trap bar deadlift")
    assert dose(deadlift) == {"mode": "reps", "sets": 4, "reps": 5, "load_or_intensity": "140kg"}
    assert deadlift["coaching_cues"] == ["drive through the floor"]
    assert deadlift["stop_rules"] == ["Stop if lower back pain"]
    assert deadlift["notes"] == "rest 2-3 min"
    assert dose(strength[1]) == {"mode": "reps", "sets": 3, "reps": 8, "per_side": True, "rpe": 7}
    # A range stays exactly as written rather than being rounded to one number.
    assert dose(strength[2]) == {"mode": "text", "prescription": "3 x 6-8"}
    assert strength[2]["notes"] == "last set to failure"
    assert dose(strength[3]) == {"mode": "duration", "sets": 3, "duration_seconds": 30}
    assert dose(pads[0]) == {"mode": "intervals", "rounds": 6, "work_seconds": 180, "rest_seconds": 60}
    assert (pads[0]["equipment"], pads[0]["coaching_cues"]) == ("16oz gloves", ["Finish every combo on the jab"])
    # Rounds with no time written: no time is invented.
    assert dose(pads[1]) == {"mode": "intervals", "rounds": 3}
    assert pads[1]["coaching_cues"] == ["head movement"]
    assert dose(road[0]) == {"mode": "distance", "distance": 5.0, "distance_unit": "km", "load_or_intensity": "zone 2"}
    assert dose(road[1]) == {"mode": "text", "prescription": "AMRAP 10 min"}
    assert dose(sparring[0]) == {"mode": "intervals", "rounds": 6, "work_seconds": 180}
    # The repeat copies week 1 exactly, a week apart.
    assert [s["scheduled_date"] for s in plan["sessions"][4:8]] == ["2026-11-30", "2026-11-30", "2026-12-02", "2026-12-05"]
    assert [e["name"] for e in plan["sessions"][4]["exercises"]] == [e["name"] for e in strength]
    assert body["flags"] == [
        {"where": "Plan", "message": "No upcoming start date written, so it starts Monday 23 Nov. Change the start date in the builder if needed."},
        {"where": "Not placed", "message": "“Sauna 2x a week if cutting”"},
    ]


def test_nothing_in_the_draft_is_invented(api):
    """Every name, load, cue, stop rule, note and number in the draft is in the coach's text."""
    client, _, monkeypatch = api
    plan = run_import(client, monkeypatch)["plan"]
    source = " ".join(PROGRAMME.split()).lower()
    numbers = set(re.findall(r"\d+(?:\.\d+)?", source))
    words = lambda value: " ".join(str(value).split()).lower()  # noqa: E731
    for session in plan["sessions"]:
        for exercise in session["exercises"]:
            name = words(exercise["name"])
            assert name in source or name.replace("rounds", "").strip() in source or name == "pad rounds", name
            for text in [exercise["dose"].get("load_or_intensity"), exercise["dose"].get("prescription"), exercise["notes"],
                         exercise["equipment"], *(exercise["coaching_cues"] or []), *(exercise["stop_rules"] or [])]:
                if text:
                    assert words(text) in source, text
            for key in ("sets", "reps", "rpe", "rounds", "distance"):
                value = exercise["dose"].get(key)
                if value is not None:
                    assert f"{value:g}" in numbers, (exercise["name"], key, value)
            # Seconds may be written as minutes ("3 min" -> 180).
            for key in ("work_seconds", "rest_seconds", "duration_seconds"):
                value = exercise["dose"].get(key)
                if value is not None:
                    assert str(value) in numbers or f"{value / 60:g}" in numbers, (exercise["name"], key, value)


def test_weekly_plans_leave_unwritten_days_open_for_the_builder_to_ask(api):
    client, _, monkeypatch = api
    reading = ReadPlan(name="", weeks=[ReadWeek(repeat=1, sessions=[
        ReadSession(name="Day 1 - Strength", session_type="strength_power", exercises=[ReadExercise(name="Squat", sets=5, reps=5)]),
        ReadSession(day="Wednesday", name="Bike", session_type="conditioning", exercises=[ReadExercise(name="Bike", sets=4, seconds=240)]),
        ReadSession(name="Day 3 - Pads", session_type="skill", exercises=[ReadExercise(name="Pads")]),
    ])])
    body = run_import(client, monkeypatch, reading)
    plan = body["plan"]
    assert plan["schedule_mode"] == "weekly" and plan["name"] == "Imported plan"
    assert sorted(str(s["weekday"]) for s in plan["sessions"]) == ["None", "None", "Wednesday"]
    assert [group["where"] for group in body["needs_day"]] == ["Day 1 - Strength", "Day 3 - Pads"]
    assert dose(next(s for s in plan["sessions"] if s["name"] == "Bike")["exercises"][0]) == {
        "mode": "duration", "sets": 4, "duration_seconds": 240}
    pads = next(s for s in plan["sessions"] if s["name"] == "Day 3 - Pads")["exercises"][0]
    assert dose(pads) == {"mode": "reps"}
    assert body["flags"] == [
        {"where": "Day 3 - Pads · Pads", "message": "No sets, reps, time or distance written. Add the dose, or leave it open."},
    ]
    # Once the person picks the days, it saves with nothing left open.
    saved = client.post("/api/custom-plans", json=place(body, "Tuesday"), headers=AUTH).json()
    assert sorted(s["weekday"] for s in saved["sessions"]) == ["Tuesday", "Tuesday", "Wednesday"]


def test_values_the_builder_cannot_hold_are_kept_as_written_or_flagged():
    flags = []
    # Out of range: flagged and kept in the notes as written, never clamped to a made-up value.
    assert dose_from(ReadExercise(name="x", sets=3, reps=5, rpe=15), flags.append).model_dump(exclude_none=True) == {
        "mode": "reps", "sets": 3, "reps": 5}
    assert flags == ["RPE 15 isn't between 1 and 10, so it's kept in the notes."]
    built = plan_import._exercise(ReadExercise(name="x", sets=3, reps=5, rpe=15, rest_seconds=-30, notes="heavy"), lambda m: None)
    assert built["notes"] == "heavy\nRPE as written: 15\nRest as written: -30s"
    # Values that don't fit one mode keep the whole written dose as text.
    assert dose_from(ReadExercise(name="x", reps=10, distance=400, distance_unit="m")).model_dump(exclude_none=True) == {
        "mode": "text", "prescription": "10 reps, 400 m"}
    assert dose_from(ReadExercise(name="x", sets=200, reps=5)).model_dump(exclude_none=True) == {
        "mode": "text", "prescription": "200 × 5"}
    assert dose_from(ReadExercise(name="x", distance=3, distance_unit="miles")).model_dump(exclude_none=True) == {
        "mode": "text", "prescription": "3 miles"}
    assert dose_from(ReadExercise(name="x", sets=3)).model_dump(exclude_none=True) == {"mode": "text", "prescription": "3 sets"}
    long = build_draft(READING.model_copy(update={"weeks": [READING.weeks[0].model_copy(update={"repeat": 20})]}), date(2026, 11, 25))
    assert long.weeks == 12
    assert long.flags[0].message == "The programme runs 20 weeks. Plans hold up to 12, so weeks 13-20 weren't brought in."
    # A Wednesday start still lays the weeks out from that week's Monday.
    assert long.plan["start_date"] == "2026-11-23"


def test_a_written_start_date_is_used_and_not_flagged():
    reading = READING.model_copy(update={"start_date": "2026-12-02"})
    result = build_draft(reading, date(2026, 11, 23))
    assert result.plan["start_date"] == "2026-11-30"
    assert not [f for f in result.flags if f.where == "Plan"]


# -- Requests --------------------------------------------------------------------

def test_only_the_programme_is_sent_and_refusals_are_plain(api):
    client, _, monkeypatch = api
    reader = FakeReader(READING)
    use(monkeypatch, reader)
    assert client.post(URL, json={"text": PROGRAMME, "start_date": MONDAY}, headers=AUTH).status_code == 200
    assert reader.texts == [PROGRAMME.strip()]
    use(monkeypatch, None)
    assert client.post(URL, json={"text": PROGRAMME, "start_date": MONDAY}, headers=AUTH).status_code == 503
    use(monkeypatch, FakeReader(READING))
    tiny = client.post(URL, json={"text": "  page 1  ", "start_date": MONDAY}, headers=AUTH)
    assert tiny.status_code == 422 and "screenshots" in tiny.json()["detail"]
    use(monkeypatch, FakeReader(ReadPlan(name="Notes", weeks=[])))
    assert client.post(URL, json={"text": PROGRAMME, "start_date": MONDAY}, headers=AUTH).status_code == 422
    use(monkeypatch, FakeReader(RuntimeError("upstream said: secret")))
    failed = client.post(URL, json={"text": PROGRAMME, "start_date": MONDAY}, headers=AUTH)
    assert failed.status_code == 502 and "secret" not in failed.text
    assert client.post(URL, json={"text": PROGRAMME, "start_date": MONDAY}).status_code == 401


def test_a_daily_limit_on_imports(api):
    client, _, monkeypatch = api
    monkeypatch.setenv("UNLXCK_PLAN_IMPORT_DAILY_LIMIT", "2")
    use(monkeypatch, FakeReader(READING))
    codes = [client.post(URL, json={"text": PROGRAMME, "start_date": MONDAY}, headers=AUTH).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 64 + b"\xff\xd9").decode()


def test_screenshots_go_to_the_model_once_and_anything_else_is_refused(api):
    client, _, monkeypatch = api
    reader = FakeReader(READING)
    use(monkeypatch, reader)
    ok = client.post(URL, json={"text": "", "images": [JPEG, JPEG], "start_date": MONDAY}, headers=AUTH)
    assert ok.status_code == 200, ok.text
    assert reader.images == [JPEG, JPEG] and reader.texts == [""]
    for bad in (["data:image/png;base64,iVBORw0KGgo="], ["data:image/jpeg;base64,not base64!"],
                ["data:image/jpeg;base64," + base64.b64encode(b"<svg/>").decode()]):
        response = client.post(URL, json={"images": bad, "start_date": MONDAY}, headers=AUTH)
        assert response.status_code == 422, bad
    too_many = client.post(URL, json={"images": [JPEG] * 5, "start_date": MONDAY}, headers=AUTH)
    assert too_many.status_code == 422
    assert len(reader.texts) == 1


def test_the_coachs_session_note_reaches_the_session_on_today(api):
    from api.services.custom_plans import dated_custom_plan_result
    from api.plan_mappers import _map_profile_row
    client, store, monkeypatch = api
    draft = place(run_import(client, monkeypatch), "Friday")
    for session in draft["sessions"]:  # The person finishes the doses the coach left open.
        for exercise in session["exercises"]:
            if exercise["dose"]["mode"] == "intervals":
                exercise["dose"].update(work_seconds=180, rest_seconds=60)
    saved = client.post("/api/custom-plans", json=draft, headers=AUTH).json()
    assert saved["incomplete"] is False
    result = dated_custom_plan_result(saved, _map_profile_row(store.profiles["athlete-1"]), training_day=MONDAY)
    monday = result["structured_plan"]["weeks"][0]["days"][0]
    strength = next(s for s in monday["sessions"] if s["title"] == "Strength")
    assert strength["objective"] == "Focus: brace hard, own every rep."


def test_the_accuracy_check_finds_no_differences_against_its_own_hand_checked_readings():
    """The real-model check (tools/plan_import_eval.py) scores a perfect reading as perfect,
    and catches a wrong day, a changed dose and an invented exercise."""
    from tools.plan_import_eval import FIXTURES as CASES, cases, compare
    found = list(cases(CASES / "eval"))
    assert [name for name, *_ in found] == ["coach_programme", "strength_block", "whatsapp_week"]
    for _, text, _, truth in found:
        assert text.strip()
        assert compare(truth, truth)["errors"] == []
    truth = found[2][3]
    wrong = truth.model_copy(deep=True)
    wrong.weeks[0].sessions[0].day = "Tuesday"
    wrong.weeks[0].sessions[2].exercises[0].reps = 6
    wrong.weeks[0].sessions[2].exercises.append(ReadExercise(name="Bicep curl", sets=3, reps=12))
    assert compare(truth, wrong)["errors"] == [
        "Pads: day expected 'Monday', read 'Tuesday'",
        "Gym · Squats: dose expected {'mode': 'reps', 'sets': 3, 'reps': 5, 'load_or_intensity': '100'}, read {'mode': 'reps', 'sets': 3, 'reps': 6, 'load_or_intensity': '100'}",
        "Gym: invented exercise 'Bicep curl'",
    ]
