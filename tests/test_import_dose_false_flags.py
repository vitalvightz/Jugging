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


@pytest.mark.parametrize("name", ["Run", "Sprint", "Swim", "Row", "Rope climb", "Unknown drill"])
def test_a_distance_misread_as_time_is_flagged_without_rewriting_it(name):
    item = ReadExercise(name=name, seconds=400)
    found = []
    SourceCheck(f"{name} 400m", []).check(item, found.append)
    assert found
    assert item.seconds == 400


def test_distance_and_short_rest_minutes_on_the_same_line():
    found = []
    SourceCheck("Run 400m, rest 5m", []).check(
        ReadExercise(name="Run", distance=400, distance_unit="m", rest_seconds=300), found.append)
    assert found == []


def test_table_header_proves_the_role_of_short_minutes():
    found = []
    source = "| Exercise | Sets x Reps | Rest |\n| --- | --- | --- |\n| Pull-ups | 4 x 5 | 2m |"
    SourceCheck(source, []).check(ReadExercise(name="Pull-ups", sets=4, reps=5, rest_seconds=120), found.append)
    assert found == []


@pytest.mark.parametrize("source,prescription", [
    ("Stretch 5 minutes", "300 reps"),
    ("Stretch 300 reps", "5 minutes"),
    ("Stretch 5 minutes, rest 1 minute", "1 minute, rest 5 minutes"),
])
def test_converted_times_cannot_validate_reps_or_the_wrong_time_role(source, prescription):
    assert flags(source, prescription=prescription)


@pytest.mark.parametrize("name,label", [
    ("Boxing session", "Boxing"), ("Technical/pads session", "Pads"),
    ("Sparring/technical rounds as scheduled", "Light technical"),
    ("Skill session", "Light technical"), ("Light technical", "Light technical"),
    ("Light sparring", "Light technical"), ("Roadwork", "Roadwork"),
    ("Hard sparring with pads", "Sparring"),
    ("Hard sparring + technical rounds", "Sparring"),
])
def test_session_activities_need_no_prescription(name, label):
    assert session_activity(name) == label
    for dose in ({"mode": "reps"}, {"mode": "intervals", "rounds": 3}):
        assert not exercise_incomplete({"name": name, "dose": dose})
    reading = ReadPlan(weeks=[ReadWeek(sessions=[ReadSession(name="Mixed", days=["Monday"], exercises=[ReadExercise(name=name)])])])
    draft = build_draft(reading, date(2026, 11, 23), name)
    assert not [f for f in draft.flags if "dose" in f.message.lower()]
    # Exempt from a dose, but never typed from its name: no stated kind, no type.
    assert draft.plan["sessions"][0]["exercises"][0]["block_type"] is None


def test_a_lift_in_a_boxing_day_still_needs_its_dose():
    assert session_activity("Box squat") is None
    assert exercise_incomplete({"name": "Back squat", "dose": {"mode": "reps"}})
    assert exercise_incomplete({"name": "Boxing push-ups", "block_type": "strength", "dose": {"mode": "reps"}})


def test_import_preserves_explicit_exercise_kind_over_name_guess():
    reading = ReadPlan(weeks=[ReadWeek(sessions=[ReadSession(name="Mixed", days=["Monday"], exercises=[
        ReadExercise(name="Pads", kind="sparring"), ReadExercise(name="Boxing push-ups", kind="strength"),
    ])])])
    draft = build_draft(reading, date(2026, 11, 23), "Pads\nBoxing push-ups")
    assert [e["block_type"] for e in draft.plan["sessions"][0]["exercises"]] == ["sparring", "strength"]
    assert any("Boxing push-ups" in f.where and "No dose" in f.message for f in draft.flags)
