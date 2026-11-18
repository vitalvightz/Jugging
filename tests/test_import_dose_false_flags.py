from datetime import date

import pytest

from api.services.custom_plans import exercise_incomplete, session_activity
from api.services.plan_import import build_draft
from api.services.plan_import_check import SourceCheck
from api.services.plan_import_reading import ReadExercise, ReadPlan, ReadSession, ReadWeek


def flags(source, **values):
    result = []
    SourceCheck(source, []).check(ReadExercise(name="Stretch", **values), result.append)
    return result


@pytest.mark.parametrize("unit", ["m", "min", "mins", "minutes"])
def test_minutes_match_seconds(unit):
    assert flags(f"Stretch 5{unit}", seconds=300) == []
    assert flags(f"Stretch 10{unit}", seconds=600) == []


def test_text_time_equivalents_and_real_mismatches():
    assert flags("Stretch 5 minutes", prescription="300s") == []
    assert flags("Stretch 300 seconds", prescription="5 mins") == []
    assert flags("Stretch 300 seconds", prescription="5m") == []
    assert flags("Stretch 5m", prescription="300s") == []
    assert flags("Stretch 5 minutes", prescription="600s")
    assert flags("Stretch 30–40 minutes", prescription="1800-2400 seconds") == []
    assert flags("Stretch 30–40 minutes", prescription="1800-2500 seconds")


@pytest.mark.parametrize("dash", ["-", "–", "—", "‑", "−"])
def test_ranges_on_shared_lines_keep_their_sets(dash):
    source = f"Strength: Push-ups 4x8{dash}10, Chin-ups 3x6{dash}10, Strict press 3x6{dash}8"
    reading = ReadPlan(weeks=[ReadWeek(sessions=[ReadSession(name="Strength", days=["Monday"], exercises=[
        ReadExercise(name="Push-ups", sets=4, prescription="8-10"),
        ReadExercise(name="Chin-ups", sets=3, prescription=f"6{dash}10"),
        ReadExercise(name="Strict press", prescription="3x6-8"),
    ])])])
    draft = build_draft(reading, date(2026, 11, 23), source)
    assert not [f for f in draft.flags if f.message.startswith("Check the dose")]
    assert draft.plan["sessions"][0]["exercises"][1]["dose"]["prescription"] == f"3 × 6{dash}10"


def test_metres_stay_a_distance():
    found = []
    SourceCheck("Run 400m", []).check(ReadExercise(name="Run", distance=400, distance_unit="m"), found.append)
    assert found == []


@pytest.mark.parametrize("name,label", [
    ("Boxing session", "Boxing"), ("Technical/pads session", "Pads"),
    ("Sparring/technical rounds as scheduled", "Light technical"),
    ("Skill session", "Light technical"), ("Light technical", "Light technical"),
    ("Light sparring", "Light technical"), ("Roadwork", "Roadwork"),
])
def test_session_activities_need_no_prescription(name, label):
    assert session_activity(name) == label
    for dose in ({"mode": "reps"}, {"mode": "intervals", "rounds": 3}):
        assert not exercise_incomplete({"name": name, "dose": dose})
    reading = ReadPlan(weeks=[ReadWeek(sessions=[ReadSession(name="Mixed", days=["Monday"], exercises=[ReadExercise(name=name)])])])
    draft = build_draft(reading, date(2026, 11, 23), name)
    assert not [f for f in draft.flags if "dose" in f.message.lower()]
    assert draft.plan["sessions"][0]["exercises"][0]["block_type"] in {"skill", "sparring", "conditioning"}


def test_a_lift_in_a_boxing_day_still_needs_its_dose():
    assert session_activity("Box squat") is None
    assert exercise_incomplete({"name": "Back squat", "dose": {"mode": "reps"}})
