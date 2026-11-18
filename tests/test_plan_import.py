"""Plan import: the text of someone's plan becomes a draft in the builder's own
format. Nothing is stored until the person saves it from the builder."""
from datetime import date

import pytest

from api.services import plan_import
from api.services.plan_import import (
    PlanImportRequest, ReadExercise, ReadPlan, ReadSession, ReadWeek, build_draft, dose_from, response_schema,
)
from tests.support import _build_client

AUTH = {"Authorization": "Bearer athlete-token"}
URL = "/api/custom-plans/import"
MONDAY = "2026-11-23"
TEXT = "Fight camp block\nWeeks 1-4\nMon Strength: Trap bar deadlift 3x5 @ 140kg, rest 2 min\nWed Pads 6 x 3 min rounds\nSat 5 km easy run"


class FakeReader:
    def __init__(self, plan: ReadPlan | Exception):
        self.plan, self.texts = plan, []

    def read(self, text, images=()):
        self.texts.append(text)
        self.images = list(images)
        if isinstance(self.plan, Exception):
            raise self.plan
        return self.plan


def camp(repeat=4):
    return ReadPlan(name="Fight camp block", start_date=None, skipped=["Drink 3L water"], weeks=[ReadWeek(repeat=repeat, sessions=[
        ReadSession(day="Monday", name="Strength", session_type="strength_power", exercises=[
            ReadExercise(name="Trap bar deadlift", sets=3, reps=5, load="140kg", rest_seconds=120),
            ReadExercise(name="Split squat", sets=3, reps=8, per_side=True),
            ReadExercise(name="Sled", prescription="EMOM 10 min, 2 lengths"),
        ]),
        ReadSession(day="Wednesday", name="Pads", session_type="skill", exercises=[
            ReadExercise(name="Pad rounds", rounds=6, work_seconds=180, rest_seconds=60, notes="Finish on the jab"),
        ]),
        ReadSession(day="Saturday", name="Roadwork", session_type="conditioning", exercises=[
            ReadExercise(name="Easy run", distance=5, distance_unit="km", load="zone 2"),
        ]),
    ])])


@pytest.fixture
def api(monkeypatch):
    client, store, _ = _build_client()
    plan_import.reset_import_limits()
    yield client, store, monkeypatch
    plan_import.reset_import_limits()
    plan_import._reader_cache.clear()


def set_(dose):
    return {key: value for key, value in dose.items() if value is not None}


def use(monkeypatch, reader):
    monkeypatch.setitem(plan_import._reader_cache, "default", reader)


def test_a_repeating_week_becomes_a_dated_draft_and_nothing_is_saved(api):
    client, store, monkeypatch = api
    reader = FakeReader(camp())
    use(monkeypatch, reader)
    response = client.post(URL, json={"text": TEXT, "file_name": "camp.pdf", "start_date": MONDAY}, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    plan = body["plan"]
    assert (body["weeks"], body["sessions"], body["exercises"]) == (4, 12, 20)
    assert plan["schedule_mode"] == "dated"
    assert (plan["start_date"], plan["end_date"]) == ("2026-11-23", "2026-12-20")
    assert [s["scheduled_date"] for s in plan["sessions"][:3]] == ["2026-11-23", "2026-11-25", "2026-11-28"]
    strength = plan["sessions"][0]["exercises"]
    assert strength[0]["origin"] == "manual" and strength[0]["name"] == "Trap bar deadlift"
    assert set_(strength[0]["dose"]) == {"mode": "reps", "sets": 3, "reps": 5, "rest_seconds": 120, "load_or_intensity": "140kg"}
    assert strength[1]["dose"]["per_side"] is True
    assert set_(strength[2]["dose"]) == {"mode": "text", "prescription": "EMOM 10 min, 2 lengths"}
    pads = plan["sessions"][1]["exercises"][0]
    assert set_(pads["dose"]) == {"mode": "intervals", "rounds": 6, "work_seconds": 180, "rest_seconds": 60}
    assert pads["notes"] == "Finish on the jab"
    assert set_(plan["sessions"][2]["exercises"][0]["dose"]) == {
        "mode": "distance", "distance": 5.0, "distance_unit": "km", "load_or_intensity": "zone 2"}
    assert body["skipped"] == ["Drink 3L water"]
    # Every session and exercise has its own id, so the builder can edit them.
    ids = [s["id"] for s in plan["sessions"]] + [e["id"] for s in plan["sessions"] for e in s["exercises"]]
    assert len(ids) == len(set(ids))
    # Only the plan text is sent: no name or email.
    assert reader.texts == [TEXT]
    assert client.get("/api/custom-plans", headers=AUTH).json() == []
    # The draft saves through the normal builder endpoint as it is.
    saved = client.post("/api/custom-plans", json=plan, headers=AUTH)
    assert saved.status_code == 201, saved.text
    assert len(saved.json()["sessions"]) == 12


def test_one_week_is_a_weekly_plan_and_unnamed_days_are_spread(api):
    client, _, monkeypatch = api
    use(monkeypatch, FakeReader(ReadPlan(name="", weeks=[ReadWeek(repeat=1, sessions=[
        ReadSession(name="Day 1", session_type="strength_power", exercises=[ReadExercise(name="Squat", sets=5, reps=5)]),
        ReadSession(name="Day 2", session_type="conditioning", exercises=[ReadExercise(name="Bike", sets=4, seconds=240)]),
        ReadSession(name="Day 3", session_type="skill"),
    ])])))
    body = client.post(URL, json={"text": TEXT, "start_date": MONDAY}, headers=AUTH).json()
    plan = body["plan"]
    assert plan["schedule_mode"] == "weekly" and plan["name"] == "Imported plan"
    assert [s["weekday"] for s in plan["sessions"]] == ["Monday", "Wednesday", "Friday"]
    assert set_(plan["sessions"][1]["exercises"][0]["dose"]) == {"mode": "duration", "sets": 4, "duration_seconds": 240}


def test_numbers_the_builder_cannot_hold_never_break_the_draft():
    assert dose_from(ReadExercise(name="x", sets=0, reps=5, rpe=15)).model_dump(exclude_none=True) == {"mode": "reps", "reps": 5}
    assert dose_from(ReadExercise(name="x", distance=3, distance_unit="miles")).model_dump(exclude_none=True) == {
        "mode": "text", "prescription": "3 miles"}
    assert dose_from(ReadExercise(name="x")).model_dump(exclude_none=True) == {"mode": "reps"}
    long = build_draft(camp(repeat=20), date(2026, 11, 25))
    assert long.weeks == 12 and long.shortened
    # A Wednesday start still lays the weeks out from that week's Monday.
    assert long.plan["start_date"] == "2026-11-23"


def test_refusals(api):
    client, _, monkeypatch = api
    use(monkeypatch, None)
    assert client.post(URL, json={"text": TEXT, "start_date": MONDAY}, headers=AUTH).status_code == 503
    use(monkeypatch, FakeReader(camp()))
    tiny = client.post(URL, json={"text": "  page 1  ", "start_date": MONDAY}, headers=AUTH)
    assert tiny.status_code == 422 and "screenshots" in tiny.json()["detail"]
    use(monkeypatch, FakeReader(ReadPlan(name="Notes", weeks=[])))
    assert client.post(URL, json={"text": TEXT, "start_date": MONDAY}, headers=AUTH).status_code == 422
    use(monkeypatch, FakeReader(RuntimeError("upstream said: secret")))
    failed = client.post(URL, json={"text": TEXT, "start_date": MONDAY}, headers=AUTH)
    assert failed.status_code == 502 and "secret" not in failed.text
    assert client.post(URL, json={"text": TEXT, "start_date": MONDAY}).status_code == 401


def test_a_daily_limit_on_imports(api):
    client, _, monkeypatch = api
    monkeypatch.setenv("UNLXCK_PLAN_IMPORT_DAILY_LIMIT", "2")
    use(monkeypatch, FakeReader(camp()))
    codes = [client.post(URL, json={"text": TEXT, "start_date": MONDAY}, headers=AUTH).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_the_schema_is_strict():
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


JPEG = "data:image/jpeg;base64," + __import__("base64").b64encode(b"\xff\xd8\xff\xe0" + b"0" * 64 + b"\xff\xd9").decode()


def test_screenshots_go_to_the_model_once_and_anything_else_is_refused(api):
    client, _, monkeypatch = api
    reader = FakeReader(camp())
    use(monkeypatch, reader)
    ok = client.post(URL, json={"text": "", "images": [JPEG, JPEG], "start_date": MONDAY}, headers=AUTH)
    assert ok.status_code == 200, ok.text
    assert reader.images == [JPEG, JPEG] and reader.texts == [""]
    for bad in (["data:image/png;base64,iVBORw0KGgo="], ["data:image/jpeg;base64,not base64!"],
                ["data:image/jpeg;base64," + __import__("base64").b64encode(b"<svg/>").decode()]):
        response = client.post(URL, json={"images": bad, "start_date": MONDAY}, headers=AUTH)
        assert response.status_code == 422, bad
    too_many = client.post(URL, json={"images": [JPEG] * 5, "start_date": MONDAY}, headers=AUTH)
    assert too_many.status_code == 422
    assert len(reader.texts) == 1
