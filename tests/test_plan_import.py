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
from api.services.plan_import import PlanImportRequest, build_draft, dose_from
from api.services.plan_import_reading import ReadCorrection, ReadExercise, ReadPlan, ReadSession, ReadWeek, response_schema
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

    def read(self, text, images=(), review=None):
        self.texts.append(text)
        self.images = list(images)
        if isinstance(self.plan, Exception):
            raise self.plan
        return self.plan


@pytest.fixture
def api(monkeypatch):
    client, store, _ = _build_client()
    plan_import.reset_import_limits()
    # Most tests are about the reading, so they run without the exercise library.
    monkeypatch.setattr(plan_import, "library_matcher", lambda store: None)
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
        {"where": "Plan", "message": "No start date written, so it starts Monday 23 Nov."},
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
    body = run_import(client, monkeypatch, reading, text="Day 1 - Strength\nSquat 5x5\nWednesday: Bike 4 x 4 min\nDay 3 - Pads")
    plan = body["plan"]
    assert plan["schedule_mode"] == "weekly" and plan["name"] == "Imported plan"
    assert sorted(str(s["weekday"]) for s in plan["sessions"]) == ["None", "None", "Wednesday"]
    assert [group["where"] for group in body["needs_day"]] == ["Day 1 - Strength", "Day 3 - Pads"]
    assert dose(next(s for s in plan["sessions"] if s["name"] == "Bike")["exercises"][0]) == {
        "mode": "duration", "sets": 4, "duration_seconds": 240}
    pads = next(s for s in plan["sessions"] if s["name"] == "Day 3 - Pads")["exercises"][0]
    assert dose(pads) == {"mode": "reps"}
    assert body["flags"] == [
        {"where": "Day 3 - Pads · Pads", "message": "No dose written. Add one or leave it open."},
    ]
    # Once the person picks the days, it saves with nothing left open.
    saved = client.post("/api/custom-plans", json=place(body, "Tuesday"), headers=AUTH).json()
    assert sorted(s["weekday"] for s in saved["sessions"]) == ["Tuesday", "Tuesday", "Wednesday"]


def test_values_the_builder_cannot_hold_are_kept_as_written_or_flagged():
    flags = []
    # Out of range: flagged and kept in the notes as written, never clamped to a made-up value.
    assert dose_from(ReadExercise(name="x", sets=3, reps=5, rpe=15), flags.append).model_dump(exclude_none=True) == {
        "mode": "reps", "sets": 3, "reps": 5}
    assert flags == ["RPE 15 isn't 1-10. It's in the notes."]
    built = plan_import._exercise(ReadExercise(name="x", sets=3, reps=5, rpe=15, rest_seconds=-30, notes="heavy"), lambda m: None)
    assert built["notes"] == "heavy\nRPE as written: 15\nRest as written: -30s"
    # Values that don't fit one mode keep the whole written dose as text.
    assert dose_from(ReadExercise(name="x", reps=10, distance=400, distance_unit="m")).model_dump(exclude_none=True) == {
        "mode": "text", "prescription": "10 reps, 400 m"}
    assert dose_from(ReadExercise(name="x", sets=200, reps=5)).model_dump(exclude_none=True) == {
        "mode": "text", "prescription": "200 × 5"}
    # Common spellings of a unit are the same unit; an unknown one stays as written.
    assert dose_from(ReadExercise(name="x", distance=3, distance_unit="miles")).model_dump(exclude_none=True) == {
        "mode": "distance", "distance": 3.0, "distance_unit": "mi"}
    assert dose_from(ReadExercise(name="x", distance=3, distance_unit="laps")).model_dump(exclude_none=True) == {
        "mode": "text", "prescription": "3 laps"}
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
    assert [name for name, *_ in found] == ["coach_programme", "boxing_clinch_3week", "boxing_prep_references", "boxing_sc_4week_dates", "boxing_sc_4week_short", "strength_block", "typos", "whatsapp_week"]
    for _, text, _, truth in found:
        assert text.strip()
        assert compare(truth, truth, text)["errors"] == []
    truth = next(f for f in found if f[0] == "whatsapp_week")[3]
    wrong = truth.model_copy(deep=True)
    wrong.weeks[0].sessions[0].day = "Tuesday"
    wrong.weeks[0].sessions[2].exercises[0].reps = 6
    wrong.weeks[0].sessions[2].exercises.append(ReadExercise(name="Bicep curl", sets=3, reps=12))
    assert compare(truth, wrong)["errors"] == [
        "Pads: day expected 'Monday', read 'Tuesday'",
        "Gym · Squats: dose expected {'mode': 'reps', 'sets': 3, 'reps': 5, 'load_or_intensity': '100'}, read {'mode': 'reps', 'sets': 3, 'reps': 6, 'load_or_intensity': '100'}",
        "Gym: invented exercise 'Bicep curl'",
    ]


# -- Edge cases ------------------------------------------------------------------

def _session(name, day=None, **kwargs):
    return ReadSession(day=day, name=name, session_type=kwargs.pop("session_type", "mixed"), **kwargs)


def test_spelling_corrections_are_listed_and_suspicious_ones_called_out():
    reading = ReadPlan(name="Block", weeks=[ReadWeek(sessions=[_session("Strength", "Monday", exercises=[
        ReadExercise(name="Bulgarian split squat", sets=3, reps=8)])])], corrections=[
        {"written": "bulgarain", "corrected": "Bulgarian"}, {"written": "sqaut", "corrected": "squat"},
        {"written": "3x8", "corrected": "3x10"},  # a number changed: never a spelling fix
        {"written": "RDL", "corrected": "Romanian deadlift"},  # not the same word
        {"written": "same", "corrected": "same"}, {"written": "", "corrected": "x"}])
    # With the programme text, a "correction" of a word that was never written is called out too.
    source = "Mon: bulgarain split sqaut 3x8, RDL 3x5"
    reading.corrections.append(ReadCorrection(written="benhc", corrected="bench"))
    assert [(f.where, f.message) for f in build_draft(reading, date(2026, 11, 23), source).flags][-2:] == [
        ("Spelling", "Check this change: “benhc” became “bench”."),
        ("Spelling", "Corrected: “bulgarain” → “Bulgarian”, “sqaut” → “squat”."),
    ]
    reading.corrections.pop()
    assert [(f.where, f.message) for f in build_draft(reading, date(2026, 11, 23)).flags] == [
        ("Spelling", "Check this change: “3x8” became “3x10”."),
        ("Spelling", "Check this change: “RDL” became “Romanian deadlift”."),
        ("Spelling", "Corrected: “bulgarain” → “Bulgarian”, “sqaut” → “squat”."),
    ]


def test_written_days_in_any_common_spelling_are_kept_and_only_real_gaps_are_asked():
    reading = ReadPlan(weeks=[ReadWeek(sessions=[
        _session("A", "thurs"), _session("B", "Weds."), _session("C", "TUE"), _session("D", "Day 2"), _session("E", "AM")])])
    result = build_draft(reading, date(2026, 11, 23))
    assert {s["name"]: s["weekday"] for s in result.plan["sessions"]} == {
        "A": "Thursday", "B": "Wednesday", "C": "Tuesday", "D": None, "E": None}
    assert [group.where for group in result.needs_day] == ["D", "E"]


def test_written_rest_weeks_keep_their_place_even_at_the_start():
    lift = _session("Lift", "Monday", exercises=[ReadExercise(name="Squat", sets=3, reps=5)])
    # Week 1 is a written recovery week, week 4 a deload; the stray empty week isn't written as rest.
    reading = ReadPlan(weeks=[ReadWeek(rest=True), ReadWeek(repeat=2, sessions=[lift]), ReadWeek(sessions=[]),
                              ReadWeek(rest=True), ReadWeek(sessions=[lift]), ReadWeek(rest=True)])
    result = build_draft(reading, date(2026, 11, 23))
    assert result.weeks == 6
    # Strength starts in week 2, as written; nothing moves earlier.
    assert [s["scheduled_date"] for s in result.plan["sessions"]] == ["2026-11-30", "2026-12-07", "2026-12-21"]
    assert (result.plan["start_date"], result.plan["end_date"]) == ("2026-11-23", "2027-01-03")
    assert [(f.where, f.message) for f in result.flags if "rest week" in f.message] == [
        ("Week 1", "Written as a rest week, so it has no sessions."), ("Week 4", "Written as a rest week, so it has no sessions."),
        ("Week 6", "Written as a rest week, so it has no sessions.")]
    # A programme that is only rest has nothing to import.
    with pytest.raises(Exception):
        build_draft(ReadPlan(weeks=[ReadWeek(rest=True)]), date(2026, 11, 23))


def test_list_labels_are_left_out_of_names_and_nameless_exercises_are_flagged_not_dropped_silently():
    reading = ReadPlan(weeks=[ReadWeek(sessions=[_session("Lower", "Monday", exercises=[
        ReadExercise(name="A1. Back squat", sets=5, reps=3), ReadExercise(name="B2) Nordic curl", sets=3, reps=5),
        ReadExercise(name="• Sled push", sets=6, distance=20, distance_unit="metres"), ReadExercise(name="3. Plank", sets=3, seconds=45),
        ReadExercise(name="T-bar row", sets=3, reps=10), ReadExercise(name="1.5 mile run", distance=1.5, distance_unit="mi"),
        ReadExercise(name="  ", sets=4, reps=6)])])])
    result = build_draft(reading, date(2026, 11, 23))
    exercises = result.plan["sessions"][0]["exercises"]
    assert [e["name"] for e in exercises] == ["Back squat", "Nordic curl", "Sled push", "Plank", "T-bar row", "1.5 mile run"]
    assert exercises[2]["dose"]["distance_unit"] == "m"
    assert [(f.where, f.message) for f in result.flags] == [
        ("Lower", "Skipped an exercise with no name: “4 × 6”.")]


def test_two_sessions_on_one_day_and_same_named_sessions_stay_separate():
    reading = ReadPlan(weeks=[ReadWeek(repeat=2, sessions=[
        _session("AM Run", "Monday", exercises=[ReadExercise(name="Run", distance=5, distance_unit="km")]),
        _session("PM Pads", "Monday", exercises=[ReadExercise(name="Pads", rounds=6, work_seconds=180, rest_seconds=60)]),
        _session("Strength", "Tuesday"), _session("Strength", None)])])
    result = build_draft(reading, date(2026, 11, 23))
    monday = [s["name"] for s in result.plan["sessions"] if s["scheduled_date"] == "2026-11-23" and s["name"] != "Strength"]
    assert monday == ["AM Run", "PM Pads"]
    # Only the Strength without a day waits for a day, once for both weeks.
    assert [(g.where, len(g.session_ids)) for g in result.needs_day] == [("Weeks 1-2 · Strength", 2)]



# -- Numbers checked against the source, not the reader's word ---------------------

def test_a_number_the_reader_changed_without_saying_is_caught_from_the_source():
    source = "Monday Strength\nBulgarian split squat 3x8 each side, RPE 7\nPads 6 x 3 min rounds, 1 min rest\n5 km easy run\nLandmine press 3 x 6-8"
    reading = ReadPlan(weeks=[ReadWeek(sessions=[_session("Strength", "Monday", exercises=[
        ReadExercise(name="Bulgarian split squat", sets=3, reps=10, per_side=True, rpe=7),  # 8 became 10, unreported
        ReadExercise(name="Pad rounds", rounds=6, work_seconds=180, rest_seconds=60),  # minutes as seconds: fine
        ReadExercise(name="Easy run", distance=5, distance_unit="km"),
        ReadExercise(name="Landmine press", prescription="3 x 6-9"),  # a range changed
        ReadExercise(name="Turkish get-up", sets=2, reps=3),  # not in the programme at all
    ])])])
    draft = build_draft(reading, date(2026, 11, 23), source)
    # A number written nowhere is fixed from the line (10 back to the written 8), not flagged.
    assert draft.plan["sessions"][0]["exercises"][0]["dose"]["reps"] == 8
    flags = [(f.where, f.message) for f in draft.flags]
    assert flags == [
        ("Strength · Landmine press", "Check the dose: 9 isn't written."),
        ("Strength · Turkish get-up", "Couldn't find its line to check the numbers. Check the dose."),
    ]


def test_the_real_programme_passes_the_source_check_with_no_false_alarms(api):
    client, _, monkeypatch = api
    flags = run_import(client, monkeypatch)["flags"]
    assert not [f for f in flags if "Check the dose" in f["message"] or "Couldn't find" in f["message"]]


def test_screenshots_say_their_numbers_could_not_be_double_checked():
    result = build_draft(READING, date(2026, 11, 23), "")
    assert ("Plan", "Read from screenshots, so doses weren't double-checked. Compare them with your screenshots.") in [
        (f.where, f.message) for f in result.flags]


def _flags_for(source, *exercises):
    reading = ReadPlan(weeks=[ReadWeek(sessions=[_session("S", "Monday", exercises=list(exercises))])])
    return [f.message for f in build_draft(reading, date(2026, 11, 23), source).flags if f.message.startswith(("Check the dose", "Couldn't"))]


def test_swapped_numbers_are_caught_by_what_they_mean_not_just_whether_they_exist():
    # 3 x 8 read as 8 sets of 3.
    assert _flags_for("Squat 3x8", ReadExercise(name="Squat", sets=8, reps=3)) == [
        "Check the dose: sets 8 is written as reps; reps 3 is written as the number of sets or rounds."]
    # 6 rounds of 3 min read as 3 rounds of 6 min.
    assert _flags_for("Pads 6 x 3 min rounds, 1 min rest", ReadExercise(name="Pads", rounds=3, work_seconds=360, rest_seconds=60)) == [
        "Check the dose: rounds 3 is written as a work time; work time 360s is written as the number of sets or rounds."]
    # Work and rest swapped.
    assert _flags_for("Pads 6 x 3 min rounds, 1 min rest", ReadExercise(name="Pads", rounds=6, work_seconds=60, rest_seconds=180)) == [
        "Check the dose: work time 60s is written as a rest time; rest 180s is written as a work time."]
    # The right reading raises nothing.
    assert _flags_for("Squat 3x8", ReadExercise(name="Squat", sets=3, reps=8)) == []
    assert _flags_for("Pads 6 x 3 min rounds, 1 min rest", ReadExercise(name="Pads", rounds=6, work_seconds=180, rest_seconds=60)) == []
    # Minutes read as seconds-by-mistake: 3 min is 180s, not 3s.
    assert _flags_for("Plank 3 x 30s", ReadExercise(name="Plank", sets=3, seconds=30)) == []
    assert _flags_for("Plank 3 x 30s", ReadExercise(name="Plank", sets=3, seconds=3)) == [
        "Check the dose: time 3s is written as the number of sets or rounds."]


def test_units_loads_and_sides_must_match_the_line():
    assert _flags_for("Easy run 5 km", ReadExercise(name="Easy run", distance=5, distance_unit="km")) == []
    assert _flags_for("Easy run 5 km", ReadExercise(name="Easy run", distance=5, distance_unit="mi")) == [
        "Check the dose: distance 5 mi has a different unit."]
    assert _flags_for("Deadlift 4x5 @ 140kg", ReadExercise(name="Deadlift", sets=4, reps=5, load="140kg")) == []
    assert _flags_for("Deadlift 4x5 @ 140kg", ReadExercise(name="Deadlift", sets=4, reps=5, load="140lb")) == [
        "Check the dose: load 140lb isn't written."]
    assert _flags_for("Lunge 3x10 each leg", ReadExercise(name="Lunge", sets=3, reps=10, per_side=True)) == []
    # Per side is set to what the line says, without a flag.
    assert _flags_for("Lunge 3x10", ReadExercise(name="Lunge", sets=3, reps=10, per_side=True)) == []
    assert _flags_for("Lunge 3x10 each leg", ReadExercise(name="Lunge", sets=3, reps=10)) == []
    assert _flags_for("Press 4x6 @ RPE 8", ReadExercise(name="Press", sets=4, reps=6, rpe=8)) == []
    assert _flags_for("Press 4x6", ReadExercise(name="Press", sets=4, reps=6, rpe=8)) == [
        "Check the dose: RPE 8 isn't written as an RPE."]
    # A range read as one number collapses what the coach wrote.
    assert _flags_for("Curl 3 x 6-8", ReadExercise(name="Curl", sets=3, reps=8)) == [
        "Check the dose: reps 8 was written as the range 6-8."]


def test_each_exercise_on_a_shared_line_is_checked_against_its_own_part():
    source = "thurs - gym: squats 3x5 @ 100, pull ups 3x8, plank 3x45s"
    ok = [ReadExercise(name="Squats", sets=3, reps=5, load="100"), ReadExercise(name="Pull ups", sets=3, reps=8),
          ReadExercise(name="Plank", sets=3, seconds=45)]
    assert _flags_for(source, *ok) == []
    # Pull ups given the squat's reps: 5 is on the line, but not in the pull-ups part.
    assert _flags_for(source, ReadExercise(name="Pull ups", sets=3, reps=5)) == [], "fixed to its own part's 8"


def test_warm_ups_cooldowns_and_mobility_need_no_dose_and_are_not_flagged_for_it(api):
    client, _, monkeypatch = api
    reading = ReadPlan(weeks=[ReadWeek(sessions=[_session("Sparring", "Friday", session_type="sparring", exercises=[
        ReadExercise(name="Hard sparring", rounds=3, work_seconds=180, rest_seconds=60),
        ReadExercise(name="Cooldown"), ReadExercise(name="Warm-up"), ReadExercise(name="Hip mobility flow", seconds=300),
        ReadExercise(name="Back squat")])])])
    body = run_import(client, monkeypatch, reading, text="Fri: Sparring 3 x 3 min, 1 min rest\nCooldown\nWarm-up\nHip mobility flow 5 min\nBack squat")
    # Only the lift with no dose is a gap; the cooldown, warm-up and mobility need none.
    assert [(f["where"], f["message"]) for f in body["flags"]] == [
        ("Sparring · Back squat", "No dose written. Add one or leave it open.")]
    saved = client.post("/api/custom-plans", json=body["plan"], headers=AUTH).json()
    incomplete = {e["name"]: e["incomplete"] for s in saved["sessions"] for e in s["exercises"]}
    assert incomplete == {"Hard sparring": False, "Cooldown": False, "Warm-up": False, "Hip mobility flow": False, "Back squat": True}


def test_a_plan_with_a_dose_free_cooldown_is_complete_and_reaches_today_without_a_dose():
    from api.services.custom_plans import _dose_text, dose_optional, exercise_incomplete, schedule_issues
    assert [dose_optional(n) for n in ("Cooldown", "Warm down", "Stretching", "Mobility", "Foam rolling", "Back squat")] == [
        True, True, True, True, True, False]
    blank = {"mode": "reps"}
    cooldown = {"name": "Cooldown", "origin": "manual", "dose": blank}
    squat = {"name": "Back squat", "origin": "manual", "dose": blank}
    assert (exercise_incomplete(cooldown), exercise_incomplete(squat)) == (False, True)
    sessions = lambda *exercises: [{"name": "Sparring", "weekday": "Friday", "session_type": "sparring", "exercises": list(exercises)}]  # noqa: E731
    assert schedule_issues(sessions(cooldown)) == []
    assert schedule_issues(sessions(cooldown, squat)) == ["Finish the dose for Back squat in Friday's Hard Sparring."]
    assert _dose_text(blank) == "" and _dose_text({"mode": "reps", "sets": 3, "reps": 5}) == "3 × 5"


def test_a_dose_free_cooldown_saves_and_compiles_into_the_plan_shown_on_today(api):
    from api.plan_mappers import _map_profile_row
    from api.services.custom_plans import dated_custom_plan_result
    client, store, monkeypatch = api
    reading = ReadPlan(weeks=[ReadWeek(repeat=2, sessions=[_session("Sparring", "Monday", session_type="sparring", exercises=[
        ReadExercise(name="Hard sparring", rounds=3, work_seconds=180, rest_seconds=60), ReadExercise(name="Cooldown")])])])
    body = run_import(client, monkeypatch, reading, text="Mon: Hard sparring 3 x 3 min, 1 min rest\\nCooldown")
    saved = client.post("/api/custom-plans", json=body["plan"], headers=AUTH)
    assert saved.status_code == 201, saved.text
    assert saved.json()["incomplete"] is False
    result = dated_custom_plan_result(saved.json(), _map_profile_row(store.profiles["athlete-1"]), training_day=MONDAY)
    blocks = result["structured_plan"]["weeks"][0]["days"][0]["sessions"][0]["blocks"]
    cooldown = next(b for b in blocks if b["display_name"] == "Cooldown")
    assert not cooldown.get("prescription_text")


# -- Matching imported exercises to the exercise library ------------------------

def _with_library(monkeypatch, media=None):
    from api.services.plan_import_match import LibraryMatcher
    from api.services.training_library import cache
    monkeypatch.setattr(plan_import, "library_matcher", lambda store: LibraryMatcher(cache.get().records, media))


def _library_names(*words):
    from api.services.training_library import cache
    return [r["entry"]["name"] for r in cache.get().records if r["summary"]["category"] == "training"
            and all(w in r["entry"]["name"].lower() for w in words)]


def test_an_exercise_with_the_library_s_name_is_linked_and_keeps_everything_written(api):
    client, _, monkeypatch = api
    _with_library(monkeypatch)
    reading = ReadPlan(weeks=[ReadWeek(sessions=[_session("Strength", "Monday", exercises=[
        ReadExercise(name="Goblet Squats", sets=3, reps=8, load="24kg", notes="Slow down", cues=["Chest up"]),
        ReadExercise(name="Mystery drill from my gym", sets=2, reps=5)])])])
    body = run_import(client, monkeypatch, reading, text="Mon: Goblet Squats 3x8 24kg\nMystery drill from my gym 2x5")
    first, second = body["plan"]["sessions"][0]["exercises"]
    assert first["origin"] == "catalogue" and body["matched"] == 1
    assert body["library"][first["id"]]["name"] == "Goblet Squat"
    assert first["dose"]["sets"] == 3 and first["dose"]["reps"] == 8 and first["dose"]["load_or_intensity"] == "24kg"
    assert first["coaching_cues"] == ["Chest up", "Slow down"], "the coach's wording stays"
    assert second["origin"] == "manual", "no library exercise like it: left as written"
    assert not [f for f in body["flags"] if "library" in f["message"]], "a same-name match needs no check"
    saved = client.post("/api/custom-plans", json=body["plan"], headers=AUTH)
    assert saved.status_code == 201, saved.text
    assert saved.json()["sessions"][0]["exercises"][0]["snapshot"]["name"] == "Goblet Squat"


def test_the_library_s_demo_video_comes_with_the_link():
    from api.models import ExerciseMedia
    from api.services.plan_import_match import LibraryMatcher
    from api.services.training_library import cache
    records = cache.get().records
    key = next(r["entry"]["exercise_key"] for r in records if r["entry"]["name"] == "Goblet Squat")
    matcher = LibraryMatcher(records, {key: ExerciseMedia(video_id="dQw4w9WgXcQ")})
    snapshot = matcher.snapshot(matcher.match("goblet squat"))
    assert snapshot["media"]["video_id"] == "dQw4w9WgXcQ"


def test_a_close_but_not_identical_name_is_linked_and_flagged_to_confirm(api):
    client, _, monkeypatch = api
    _with_library(monkeypatch)
    reading = ReadPlan(weeks=[ReadWeek(sessions=[_session("Skill", "Tuesday", exercises=[
        ReadExercise(name="Grapplers Flow Roll", rounds=3, work_seconds=300, rest_seconds=60)])])])
    body = run_import(client, monkeypatch, reading, text="Tue: Grapplers Flow Roll 3 x 5 min, 1 min rest")
    assert body["matched"] == 1
    assert [(f["where"], f["message"]) for f in body["flags"]] == [
        ("Skill \u00b7 Grapplers Flow Roll", "Matched to the library exercise \u201cGrappler's Flow Roll\u201d. Check it's the same exercise.")]


def test_names_that_differ_in_a_number_or_are_ambiguous_are_never_linked():
    from api.services.plan_import_match import LibraryMatcher
    from api.services.training_library import cache
    matcher = LibraryMatcher(cache.get().records)
    isometrics = _library_names("front squat isometric")
    assert isometrics, "the library has numbered variants to guard against"
    assert matcher.match(isometrics[0]) is not None
    assert matcher.match(isometrics[0].replace("105", "110").replace("115", "120")) is None
    assert matcher.match("Cooldown") is None and matcher.match("") is None


def test_dose_optional_work_is_complete_only_while_nothing_is_written():
    from api.services.custom_plans import exercise_incomplete, schedule_issues

    def ex(name, **dose):
        return {"name": name, "origin": "manual", "dose": {"mode": "reps", **dose}}
    assert exercise_incomplete(ex("Warm-up")) is False
    assert exercise_incomplete(ex("Warm-up squats")) is False
    assert exercise_incomplete(ex("Warm-up squats", sets=3)) is True, "3 sets but no reps"
    assert exercise_incomplete(ex("Mobility flow", mode="duration", duration_seconds=300)) is False
    assert exercise_incomplete(ex("Mobility flow", mode="intervals", rounds=3)) is True
    assert exercise_incomplete(ex("Cooldown", per_side=False, prescription="")) is False
    sessions = [{"name": "Strength", "weekday": "Monday", "session_type": "strength_power",
                 "exercises": [ex("Warm-up squats", sets=3)]}]
    assert schedule_issues(sessions) == ["Finish the dose for Warm-up squats in Monday's Strength & Power."]


def test_an_imported_warm_up_with_a_written_dose_is_treated_like_any_exercise(api):
    client, _, monkeypatch = api
    reading = ReadPlan(weeks=[ReadWeek(sessions=[_session("Strength", "Monday", session_type="strength_power", exercises=[
        ReadExercise(name="Warm-up squats", sets=3), ReadExercise(name="Back squat", sets=3)])])])
    body = run_import(client, monkeypatch, reading, text="Mon: Warm-up squats 3 sets\nBack squat 3 sets")
    warm, squat = body["plan"]["sessions"][0]["exercises"]
    assert warm["dose"] == squat["dose"], "a name never changes how a written dose is read"
    saved = client.post("/api/custom-plans", json=body["plan"], headers=AUTH).json()
    assert [e["incomplete"] for e in saved["sessions"][0]["exercises"]] == [False, False]



# -- Regression: a three-week boxing programme (tests/fixtures/plan_import/eval) ---

EVAL = Path(__file__).parent / "fixtures" / "plan_import" / "eval"


def _fixture_draft(name):
    reading = ReadPlan.model_validate_json((EVAL / f"{name}.json").read_text())
    source = (EVAL / f"{name}.txt").read_text()
    draft = build_draft(reading, date.fromisoformat(MONDAY), source)
    CustomPlanInput.model_validate(draft.plan)
    weeks: dict[int, dict[str, list[dict]]] = {}
    for s in draft.plan["sessions"]:
        when = date.fromisoformat(s["scheduled_date"])
        week = (when - date.fromisoformat(draft.plan["start_date"])).days // 7
        weeks.setdefault(week, {}).setdefault(WEEKDAYS[when.weekday()], []).append(s)
    return draft, weeks


def _dose(exercise):
    return {k: v for k, v in exercise["dose"].items() if v is not None}


def test_several_days_written_together_are_each_scheduled():
    from api.services.plan_import_reading import _days
    assert _days("Tue/Thu/Sat") == ["Tuesday", "Thursday", "Saturday"]
    assert _days("Monday/Wed") == ["Monday", "Wednesday"]
    assert _days("Mon, Wed & Fri") == ["Monday", "Wednesday", "Friday"]
    assert _days("Monday-Friday") == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    assert _days("Day 1") == [] and _days("AM") == []
    # An older reading with the days in one string is read the same way.
    reading = ReadPlan(weeks=[ReadWeek(sessions=[_session("Boxing", "Tue/Thu/Sat", session_type="skill", exercises=[
        ReadExercise(name="Pads", rounds=6, work_seconds=180, rest_seconds=60)])])])
    draft = build_draft(reading, date.fromisoformat(MONDAY), None)
    assert [s["weekday"] for s in draft.plan["sessions"]] == ["Tuesday", "Thursday", "Saturday"]
    assert draft.needs_day == [], "days written are never asked for"


def test_the_three_week_boxing_programme_keeps_its_schedule():
    draft, weeks = _fixture_draft("boxing_clinch_3week")
    assert draft.weeks == 3 and sorted(weeks) == [0, 1, 2]
    assert draft.needs_day == []
    for week in weeks.values():
        # Tue/Thu/Sat boxing every week, Sunday rest every week, nothing else added.
        assert sorted(week) == sorted(WEEKDAYS)
        for day in ("Tuesday", "Thursday", "Saturday"):
            assert [s["name"] for s in week[day]] == ["Boxing skill work, sparring, and low-intensity aerobic roadwork"]
        assert [(s["session_type"], s["exercises"]) for s in week["Sunday"]] == [("rest", [])]
        # The overview's Monday/Wednesday/Friday lines don't double the week's own sessions.
        assert all(len(week[day]) == 1 for day in ("Monday", "Wednesday", "Friday"))
    names = {w: {d: [e["name"] for e in week[d][0]["exercises"]] for d in ("Monday", "Wednesday", "Friday")} for w, week in weeks.items()}
    assert names[0]["Monday"][0] == "Neutral-Grip Floor Press or Swiss Bar Bench"
    assert names[1]["Monday"][0] == "Weighted Neutral-Grip Pull-Ups"
    assert names[2]["Monday"][0] == "Speed Floor Press"
    assert names[1]["Wednesday"][0] == names[0]["Wednesday"][0] == "Trap Bar Deadlift"
    assert len({tuple(names[w]["Friday"]) for w in weeks}) == 3


def test_the_boxing_programme_keeps_each_week_s_own_prescriptions():
    _, weeks = _fixture_draft("boxing_clinch_3week")
    deadlift = [_dose(weeks[w]["Wednesday"][0]["exercises"][0]) for w in (0, 1)]
    assert deadlift == [{"mode": "reps", "sets": 4, "reps": 5}, {"mode": "reps", "sets": 4, "reps": 3, "load_or_intensity": "85–88% 1RM"}]
    taper = {e["name"]: _dose(e) for e in weeks[2]["Wednesday"][0]["exercises"]}
    assert taper["Zercher Carry"] == {"mode": "distance", "sets": 2, "distance": 20, "distance_unit": "m", "load_or_intensity": "Moderate weight, brisk pace"}
    assert taper["Reverse Lunges"]["sets"] == 2 and taper["Reverse Lunges"]["reps"] == 5 and taper["Reverse Lunges"]["per_side"] is True
    week1_zercher = next(e for e in weeks[0]["Wednesday"][0]["exercises"] if e["name"] == "Zercher Carry")
    assert _dose(week1_zercher) == {"mode": "distance", "sets": 3, "distance": 30, "distance_unit": "m"}


def test_repeated_conditioning_blocks_and_timed_rounds_are_kept_not_flattened():
    draft, weeks = _fixture_draft("boxing_clinch_3week")
    inside = weeks[0]["Friday"][0]["exercises"][-1]
    assert _dose(inside) == {"mode": "intervals", "rounds": 6, "work_seconds": 15, "rest_seconds": 45}
    assert "Repeat: 2 blocks of these rounds, 2 min rest between blocks" in inside["notes"]
    championship = weeks[1]["Friday"][0]["exercises"][-1]
    assert _dose(championship) == {"mode": "intervals", "rounds": 3, "work_seconds": 180, "rest_seconds": 60}
    stages = championship["notes"].split("Each round, in order:\n")[1].split("\n")
    assert [line[:9] for line in stages] == ["0:00–0:30", "0:30–1:00", "1:00–2:00", "2:00–2:30", "2:30–3:00"]
    messages = [f.message for f in draft.flags]
    assert "Written as 2 blocks of 6 rounds with 2 min rest between blocks. The dose is one block; the repeat is in the notes." in messages
    assert "5 timed parts per round, listed in the notes." in messages
    # A circuit's rounds are each exercise's sets, read off the circuit heading.
    finisher = {e["name"]: _dose(e) for e in weeks[0]["Monday"][0]["exercises"] if "Clinch Finisher" in (e["notes"] or "")}
    assert finisher["Heavy Sandbag Bear-Hug Hold"] == {"mode": "duration", "sets": 3, "duration_seconds": 30}


def test_the_boxing_programme_passes_its_own_source_check_with_nothing_invented():
    draft, weeks = _fixture_draft("boxing_clinch_3week")
    assert not [f for f in draft.flags if f.message.startswith(("Check the dose", "Couldn't find", "Written twice"))]
    # Only written numbers: every dose number appears in the programme text.
    source = (EVAL / "boxing_clinch_3week.txt").read_text()
    for week in weeks.values():
        for sessions in week.values():
            for exercise in sessions[0]["exercises"]:
                for key in ("sets", "reps", "rounds", "distance"):
                    if exercise["dose"].get(key):
                        assert f"{exercise['dose'][key]:g}" in source, (exercise["name"], key)


def test_a_wrong_reading_of_the_boxing_programme_is_caught():
    reading = ReadPlan.model_validate_json((EVAL / "boxing_clinch_3week.json").read_text())
    source = (EVAL / "boxing_clinch_3week.txt").read_text()
    wrong = reading.model_copy(deep=True)
    # Week 3's taper read with week 1's deadlift numbers, and the two blocks folded into 12 rounds.
    wrong.weeks[2].sessions[1].exercises[0].sets = 3
    friday = wrong.weeks[0].sessions[2].exercises[-1]
    friday.rounds, friday.blocks, friday.block_rest_seconds = 12, None, None
    flags = {f.where: f.message for f in build_draft(wrong, date.fromisoformat(MONDAY), source).flags}
    assert flags["Week 3 \u00b7 Lower Ballistics & Core \u00b7 Trap Bar Jump Squats"].startswith("Check the dose: sets 3")
    assert "only one block was read" in flags["Week 1 \u00b7 Rotational Explosiveness & Pocket Conditioning \u00b7 \"Inside Fighting\" Conditioning"]


def test_the_same_session_written_twice_on_a_day_is_brought_in_once():
    boxing = _session("Boxing", "Tuesday", session_type="skill", exercises=[ReadExercise(name="Pads", rounds=6, work_seconds=180, rest_seconds=60)])
    reading = ReadPlan(weeks=[ReadWeek(sessions=[boxing, boxing.model_copy(deep=True)])])
    draft = build_draft(reading, date.fromisoformat(MONDAY), None)
    assert len(draft.plan["sessions"]) == 1
    assert [f.message for f in draft.flags] == ["Written twice on Tuesday, so it was brought in once."]


# -- Regression: references to earlier days ("Same as Day 1") ---------------------

def test_same_as_day_references_copy_the_named_day_and_keep_the_change_as_a_note():
    draft, weeks = _fixture_draft("boxing_prep_references")
    week = weeks[0]
    names = {day: [e["name"] for e in week[day][0]["exercises"]] for day in week}
    # No exercise is ever a reference.
    assert not [n for ns in names.values() for n in ns if re.match(r"(?i)(same as|repeat|as above)", n)]
    # Day 3's warm-up is Day 1's warm-up; its mobility is Day 1's ankle and hip mobility.
    assert names["Wednesday"] == ["Jumping jacks", "Arm swings, hip & ankle rolls", "High knees or march",
                                  "Ankle mobility & hip mobility YouTube routines", "Mirror stance, jab, cross"]
    day1 = {e["name"]: e for e in week["Monday"][0]["exercises"]}
    day3 = {e["name"]: e for e in week["Wednesday"][0]["exercises"]}
    assert day3["Jumping jacks"]["dose"] == day1["Jumping jacks"]["dose"]
    assert "As Day 1 (Warm-up)" in day3["Jumping jacks"]["notes"]
    # The section it's written under keeps its own time; no exercise gets one invented.
    assert _dose(day3["Ankle mobility & hip mobility YouTube routines"]) == {"mode": "text", "prescription": "Within Mobility Flow (15 mins)"}
    # Day 4 is Day 2 in full, with the coach's progression note and no new numbers.
    assert names["Thursday"] == names["Tuesday"]
    for d2, d4 in zip(week["Tuesday"][0]["exercises"], week["Thursday"][0]["exercises"]):
        assert d4["dose"] == d2["dose"]
        assert "As Day 2" in d4["notes"] and "Progression: increase reps slightly if easier" in d4["notes"]
    assert names["Saturday"] == [] and week["Sunday"][0]["session_type"] == "rest"
    assert [f.message for f in draft.flags if "Plan" != f.where] == []


def test_section_durations_are_kept_without_inventing_exercise_times():
    _, weeks = _fixture_draft("boxing_prep_references")
    core = [e for e in weeks[0]["Friday"][0]["exercises"] if e["name"] in ("Bicycle crunch", "Mountain climbers", "Plank", "Leg raises")]
    assert [_dose(e) for e in core] == [{"mode": "text", "prescription": "Within Core Routine (15 mins)"}] * 4
    assert all("Part of: Core Routine (15 mins)" in e["notes"] for e in core)


def test_a_reference_that_cant_be_resolved_or_is_ambiguous_is_flagged_not_guessed():
    def plan(*sessions):
        return ReadPlan(weeks=[ReadWeek(sessions=list(sessions))])
    day1 = _session("Day 1 - Mobility", "Monday", exercises=[
        ReadExercise(name="Hip circles", seconds=60, group="Warm-up"), ReadExercise(name="Ankle rocks", seconds=60, group="Mobility")])
    missing = _session("Day 3", "Wednesday", exercises=[ReadExercise(name="Same as Day 7", refers_to_day="Day 7")])
    draft = build_draft(plan(day1, missing), date.fromisoformat(MONDAY), None)
    assert [e["name"] for e in draft.plan["sessions"][1]["exercises"]] == []
    assert any("Couldn't find Day 7" in f.message for f in draft.flags)
    # "Hip + ankle drills" could be the warm-up or the mobility: never guessed.
    unclear = _session("Day 3", "Wednesday", exercises=[ReadExercise(name="Repeat hip + ankle drills", refers_to_section="hip + ankle drills")])
    day1b = _session("Day 1 - Mobility", "Monday", exercises=[
        ReadExercise(name="Hip and ankle circles", seconds=60, group="Warm-up"), ReadExercise(name="Hip and ankle rocks", seconds=60, group="Mobility")])
    draft = build_draft(plan(day1b, unclear), date.fromisoformat(MONDAY), None)
    assert draft.plan["sessions"][1]["exercises"] == []
    assert any("matches more than one earlier part" in f.message for f in draft.flags)
    # A change with numbers is kept and flagged, never applied.
    numbered = _session("Day 4", "Thursday", exercises=[ReadExercise(name="Same as Day 1 but 4 rounds", refers_to_day="Day 1", notes="but 4 rounds")])
    draft = build_draft(plan(day1, numbered), date.fromisoformat(MONDAY), None)
    copied = draft.plan["sessions"][1]["exercises"]
    assert [e["dose"]["duration_seconds"] for e in copied] == [60, 60]
    assert any("Copied from" in f.message for f in draft.flags)


def test_a_missing_week_or_a_day_the_programme_never_names_is_flagged():
    from api.services.plan_import_check import written_layout_flags
    found = []
    written_layout_flags("Week 1\nMon: squats 3x5\nWeek 2\nMon: squats 3x3\nWeek 3\nMon - Fri: walk 20 min", 2,
                         {"Monday", "Wednesday", "Sunday"}, lambda where, message: found.append(message))
    assert found == ["The programme mentions Week 3, but only 2 weeks were read.",
                     "A session was placed on Sunday, which the programme never names. Check its day."]


def test_both_regression_programmes_save_and_reload_as_custom_plans(api):
    client, _, _ = api
    for name in ("boxing_clinch_3week", "boxing_prep_references"):
        draft, _ = _fixture_draft(name)
        saved = client.post("/api/custom-plans", json=draft.plan, headers=AUTH)
        assert saved.status_code == 201, saved.text
        body = saved.json()
        assert len(body["sessions"]) == 21
        assert {s["session_type"] for s in body["sessions"] if not s["exercises"]} <= {"rest", "recovery"}


def test_a_reference_uses_this_week_then_the_nearest_earlier_week_and_flags_a_tie():
    def monday(name, sets):
        return _session(name, "Monday", exercises=[ReadExercise(name="Back squat", sets=sets, reps=5)])

    def same_as_monday(day="Wednesday"):
        return _session("Repeat", day, exercises=[ReadExercise(name="Same as Monday", refers_to_day="Monday")])
    reading = ReadPlan(weeks=[ReadWeek(sessions=[monday("Week 1 Monday", 3)]),
                              ReadWeek(sessions=[monday("Week 2 Monday", 4)]),
                              ReadWeek(sessions=[monday("Week 3 Monday", 5), same_as_monday()]),
                              ReadWeek(sessions=[same_as_monday()])])
    draft = build_draft(reading, date.fromisoformat(MONDAY), None)
    copies = [s["exercises"][0]["dose"]["sets"] for s in draft.plan["sessions"] if s["name"] == "Repeat"]
    # Week 3 copies its own Monday; week 4 has none, so the nearest earlier week's (week 3).
    assert copies == [5, 5]
    # Two Mondays in the nearest week: never guessed.
    tied = ReadPlan(weeks=[ReadWeek(sessions=[monday("AM lift", 3), monday("PM lift", 4)]), ReadWeek(sessions=[same_as_monday()])])
    draft = build_draft(tied, date.fromisoformat(MONDAY), None)
    assert [s["exercises"] for s in draft.plan["sessions"] if s["name"] == "Repeat"] == [[]]
    assert any("matches more than one Monday" in f.message for f in draft.flags)



# -- Regression: a dated 4-week plan whose later weeks refer back ("Same as Week 1") --

def test_dated_weeks_that_refer_back_copy_the_right_session_on_the_written_dates():
    draft, _ = _fixture_draft("boxing_sc_4week_dates")
    sessions = draft.plan["sessions"]
    # Every session on its written date, Wednesday to Monday weeks included.
    assert [s["scheduled_date"] for s in sessions] == [
        "2026-11-18", "2026-11-20", "2026-11-23", "2026-11-25", "2026-11-27", "2026-11-30",
        "2026-12-02", "2026-12-04", "2026-12-07", "2026-12-09", "2026-12-11", "2026-12-14"]
    names = [[e["name"] for e in s["exercises"]] for s in sessions]
    assert not [n for ns in names for n in ns if re.match(r"(?i)(same|adjustment|repeat)", n)]
    # Weeks 2-4 repeat week 1's own sessions, never another day's.
    for week in range(1, 4):
        assert names[3 * week:3 * week + 3] == names[0:3]
    week1_day1 = [_dose(e) for e in sessions[0]["exercises"]]
    for later in (3, 6):
        assert [_dose(e) for e in sessions[later]["exercises"]] == week1_day1, "no set change written: doses as copied"
    # Week 4's "Cut 1 set from every exercise" is exact, so it is applied (4 -> 3, 3 -> 2);
    # "drop lift weights by 15%" stays in the notes.
    week1_sets = [e["dose"].get("sets") for e in sessions[0]["exercises"]]
    week4_sets = [e["dose"].get("sets") for e in sessions[9]["exercises"]]
    assert week4_sets == [s - 1 if s else s for s in week1_sets]
    deload = sessions[9]["exercises"][0]["notes"]
    assert "drop lift weights by 15%" in deload
    assert "Add 2.5–5 kg" not in deload, "an earlier week's adjustment doesn't carry into the next copy"
    assert any(f.where.startswith("Week 4 \u00b7 Day 1") and "Cut 1 set" in f.message for f in draft.flags)
    assert not [f for f in draft.flags if f.message.startswith(("Check the dose", "Couldn't find", "No sets"))]


def test_references_the_reader_returned_as_plain_exercises_are_still_resolved():
    """As read live: "Same workout as Nov 20" and "Same 3-min round intervals" came back as
    exercises with no dose and no reference fields. They are references all the same."""
    reading = ReadPlan.model_validate_json((EVAL / "boxing_sc_4week_dates.json").read_text())
    for week in reading.weeks[1:]:
        for number, session in enumerate(week.sessions):
            for item in session.exercises:
                item.refers_to_day = item.refers_to_section = None
                item.name = ["Same workout as Nov 18", "Same workout as Nov 20", "Same 3-min round intervals"][number]
    draft = build_draft(reading, date.fromisoformat(MONDAY), (EVAL / "boxing_sc_4week_dates.txt").read_text())
    names = [[e["name"] for e in s["exercises"]] for s in draft.plan["sessions"]]
    assert names[3:6] == names[6:9] == names[9:12] == names[0:3]
    assert not [n for ns in names for n in ns if n.lower().startswith("same")]


# -- Written progressions on copied sessions --

def test_progression_reads_only_exact_dose_changes():
    from api.services.plan_import_progression import read_progression
    assert read_progression("Cut 1 set from every exercise and drop lift weights by 15%").sets_delta == -1
    assert read_progression("one set fewer").sets_delta == -1
    assert read_progression("Add 2 sets").sets_delta == 2
    assert read_progression("Adjustment: Increase volume to 5 rounds.").rounds == 5
    assert read_progression("Taper volume down to 3 sharp rounds").rounds == 3
    for vague in ("increase weight if comfortable", "drop weights by 15%", "increase reps slightly if easier", None):
        assert not read_progression(vague)
    assert not read_progression("add 1 set, then cut 1 set"), "conflicting changes are never applied"


def test_progression_never_drops_a_dose_below_one():
    from api.services.plan_import_progression import Progression, apply_progression
    one = ReadExercise(name="Neck holds", sets=1)
    assert apply_progression(one, Progression(sets_delta=-1)) == [] and one.sets == 1


def test_dated_conditioning_follows_the_written_round_progression():
    draft, _ = _fixture_draft("boxing_sc_4week_dates")
    rounds = [e["dose"].get("rounds") for s in draft.plan["sessions"] for e in s["exercises"] if e["dose"].get("rounds")]
    assert rounds == [4, 5, 6, 3]


# -- Regression: the short dated plan as imported live (no year, bare "Same workout") --

def test_short_dated_plan_without_a_year_copies_every_week():
    draft, _ = _fixture_draft("boxing_sc_4week_short")
    sessions = draft.plan["sessions"]
    assert draft.plan["start_date"] == "2026-11-16"
    assert [s["scheduled_date"] for s in sessions] == [
        "2026-11-18", "2026-11-20", "2026-11-23", "2026-11-25", "2026-11-27", "2026-11-30",
        "2026-12-02", "2026-12-04", "2026-12-07", "2026-12-09", "2026-12-11", "2026-12-14"]
    names = [[e["name"] for e in s["exercises"]] for s in sessions]
    for week in range(1, 4):
        assert names[3 * week:3 * week + 3] == names[0:3], f"week {week + 1} copies week 1"
    def sets(session):
        return [e["dose"].get("sets") for e in session["exercises"]]
    assert sets(sessions[9]) == [n - 1 for n in sets(sessions[0])] and sets(sessions[10]) == [n - 1 for n in sets(sessions[1])]
    assert [s["exercises"][0]["dose"].get("rounds") for s in sessions[2::3]] == [4, 5, 6, 3]
    messages = " ".join(f.message for f in draft.flags)
    for wrong in ("Couldn't find", "couldn't be matched", "No start date", "per side isn't written", "only one block was read"):
        assert wrong not in messages


def test_the_source_check_quotes_the_exercise_line_not_a_title_or_coaching_note():
    """As imported live: Plank quoted the title ("...Plan"), band rotations quoted a
    coaching note further down, and "2x30s each" was flagged as not per side."""
    source = """Strength & Conditioning Plan – 2 Weeks (Boxer)
Day 1 – Upper Body Strength
• Band external rotations 3x12 each side
• Bench press 4x6
• Core: plank 3x45s, side plank 2x30s each
Notes
• Shoulder health is non-negotiable for a boxer: face pulls, external rotations, and band work every upper day."""
    reading = ReadPlan.model_validate({"name": "S&C", "weeks": [{"repeat": 1, "sessions": [{
        "days": ["Monday"], "name": "Day 1 – Upper Body Strength", "session_type": "strength_power", "exercises": [
            {"name": "Band external rotations", "sets": 3, "reps": 12, "per_side": True},
            {"name": "Bench press", "sets": 4, "reps": 6},
            {"name": "Plank", "sets": 3, "seconds": 45},
            {"name": "side plank", "sets": 2, "seconds": 30, "per_side": True}]}]}]})
    draft = build_draft(reading, date.fromisoformat(MONDAY), source)
    assert not [f.message for f in draft.flags if "Check the dose" in f.message or "Couldn't find" in f.message]



# -- Second look: one retry with the failed checks, kept only if it is better --

class TwoReadings:
    def __init__(self, first, second):
        self.readings, self.reviews = [first, second], []

    def read(self, text, images=(), review=None):
        self.reviews.append(review)
        return self.readings[len(self.reviews) - 1]


def _one_exercise(sets, reps):
    return ReadPlan.model_validate({"name": "Plan", "weeks": [{"repeat": 1, "sessions": [{
        "days": ["Monday"], "name": "Strength", "session_type": "strength_power",
        "exercises": [{"name": "Bench press", "sets": sets, "reps": reps}]}]}]})


def _import(reader, text="Monday Strength\nBench press 4x6\n"):
    body = plan_import.PlanImportRequest(text=text, start_date=date.fromisoformat(MONDAY))
    return plan_import.import_plan("owner", body, reader=reader)


def test_a_reading_that_fails_its_checks_gets_one_second_look_and_keeps_the_better_one():
    plan_import.reset_import_limits()
    reader = TwoReadings(_one_exercise(6, 4), _one_exercise(4, 6))  # 4x6 read as 6 sets of 4
    result = _import(reader)
    assert reader.reviews[0] is None and "sets 6" in reader.reviews[1]
    assert result.plan["sessions"][0]["exercises"][0]["dose"]["reps"] == 6
    assert not [f for f in result.flags if f.message.startswith("Check the dose")]


def test_a_second_look_that_is_no_better_is_dropped_and_a_clean_reading_gets_none():
    plan_import.reset_import_limits()
    reader = TwoReadings(_one_exercise(6, 4), _one_exercise(6, 4))
    assert _import(reader).plan["sessions"][0]["exercises"][0]["dose"]["reps"] == 4
    clean = TwoReadings(_one_exercise(4, 6), None)
    _import(clean)
    assert clean.reviews == [None], "no second call when every check passes"

def test_a_written_tempo_goes_in_the_builders_tempo_field():
    reading = ReadPlan.model_validate({"name": "Plan", "weeks": [{"repeat": 1, "sessions": [{
        "days": ["Monday"], "name": "Strength", "session_type": "strength_power",
        "exercises": [{"name": "Back squat", "sets": 4, "reps": 5, "tempo": "3-1-1-0"},
                      {"name": "Bench press", "sets": 3, "reps": 8, "tempo": "x" * 50}]}]}]})
    exercises = build_draft(reading, date.fromisoformat(MONDAY)).plan["sessions"][0]["exercises"]
    assert exercises[0]["dose"]["tempo"] == "3-1-1-0"
    assert exercises[1]["dose"].get("tempo") is None and "Tempo: " + "x" * 50 in exercises[1]["notes"]


def test_each_exercise_keeps_its_own_kind_whatever_the_session_is():
    """As imported live: sparring and bag rounds in a "Boxing + Conditioning" day showed as Accessory."""
    reading = ReadPlan.model_validate({"name": "Plan", "weeks": [{"repeat": 1, "sessions": [{
        "days": ["Saturday"], "name": "Day 4 – Boxing + Conditioning", "session_type": "mixed", "exercises": [
            {"name": "Jump rope", "seconds": 300, "kind": "warm_up"},
            {"name": "Sparring/technical rounds", "prescription": "as scheduled at gym", "kind": "skill"},
            {"name": "Hard sparring", "rounds": 6, "work_seconds": 180, "kind": "sparring"},
            {"name": "bag work", "sets": 6, "seconds": 30, "kind": "skill"},
            {"name": "Stretch", "kind": None}]}]}]})
    plan = build_draft(reading, date.fromisoformat(MONDAY)).plan
    CustomPlanInput.model_validate(plan)
    assert [e.get("block_type") for e in plan["sessions"][0]["exercises"]] == [
        "preparation", "skill", "sparring", "skill", None]


def test_a_number_is_fixed_from_its_own_line_or_left_and_flagged():
    """As imported live: jump rope got 180s from nowhere under "Warm-up (10 min)"."""
    source = "Day 1 – Upper Body Strength\n• Warm-up (10 min): Jump rope, leg swings, hip openers, med ball slams 2×8\n• Plank 3x45s"
    reading = ReadPlan(weeks=[ReadWeek(sessions=[_session("Day 1", "Monday", exercises=[
        ReadExercise(name="Jump rope", seconds=180, group="Warm-up (10 min)"),
        ReadExercise(name="Plank", sets=3, seconds=30)])])])
    draft = build_draft(reading, date(2026, 11, 23), source)
    jump, plank = draft.plan["sessions"][0]["exercises"]
    # No time of its own is written, so it is left as read and flagged (never given the heading's 10 min).
    assert jump["dose"]["duration_seconds"] == 180
    assert plank["dose"]["duration_seconds"] == 45, "fixed from its own line"
    assert [f.message for f in draft.flags if f.message.startswith("Check the dose")] == ["Check the dose: time 180s isn't written."]


def test_a_second_look_that_drops_an_exercise_is_never_kept():
    """Dropping the hard-to-read exercise would make the second reading look cleaner."""
    plan_import.reset_import_limits()
    first = ReadPlan.model_validate({"name": "Plan", "weeks": [{"repeat": 1, "sessions": [{
        "days": ["Monday"], "name": "Strength", "session_type": "strength_power", "exercises": [
            {"name": "Bench press", "sets": 6, "reps": 4}, {"name": "Row", "sets": 3, "reps": 8}]}]}]})
    reader = TwoReadings(first, _one_exercise(4, 6))  # fixed the bench press, but lost the row
    result = _import(reader, "Monday Strength\nBench press 4x6\nRow 3x8\n")
    assert len(reader.reviews) == 2
    assert [e["name"] for e in result.plan["sessions"][0]["exercises"]] == ["Bench press", "Row"]


def test_the_exercise_line_with_a_dose_beats_a_heading_that_names_it():
    source = "Day 1 – Plank\nPlank 3x45s"
    assert _flags_for(source, ReadExercise(name="Plank", sets=3, seconds=45)) == []
    reading = ReadPlan(weeks=[ReadWeek(sessions=[_session("S", "Monday", exercises=[ReadExercise(name="Plank", sets=3, seconds=30)])])])
    assert build_draft(reading, date(2026, 11, 23), source).plan["sessions"][0]["exercises"][0]["dose"]["duration_seconds"] == 45
