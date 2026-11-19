"""Sport-agnostic source fidelity. Readings are hand-authored model doubles.

Each programme uses a different discipline, layout and notation. A faithful
reading must pass without dose flags; a value borrowed from another week or
session, or invented, must be flagged on that exact exercise. No discipline
gets its own rules: the same source check applies to all of them.
"""
from datetime import date

import pytest

from api.services.plan_import import build_draft
from api.services.plan_import_reading import ReadExercise as E, ReadPlan, ReadSession as S, ReadWeek as W

START = date(2026, 11, 23)


def dose_flags(result, exercise_id=None):
    return [f for f in result.flags if f.exercise_id and (exercise_id is None or f.exercise_id == exercise_id)]


# Strength: percentage loading in a table, repeated lift across two weeks.
STRENGTH = """Strength Block

Week 1
Monday - Lower
| Exercise | Sets | Reps | Load |
| --- | --- | --- | --- |
| Back squat | 5 | 5 | 75% |
| Romanian deadlift | 3 | 8 | 60kg |

Week 2
Monday - Lower
| Exercise | Sets | Reps | Load |
| --- | --- | --- | --- |
| Back squat | 4 | 3 | 85% |
| Romanian deadlift | 3 | 6 | 70kg |
"""


def strength(w2_squat=None):
    return ReadPlan(name="Strength Block", weeks=[
        W(week_index=1, sessions=[S(name="Lower", days=["Monday"], exercises=[
            E(name="Back squat", sets=5, reps=5, load="75%"), E(name="Romanian deadlift", sets=3, reps=8, load="60kg")])]),
        W(week_index=2, sessions=[S(name="Lower", days=["Monday"], exercises=[
            E(name="Back squat", **(w2_squat or {"sets": 4, "reps": 3, "load": "85%"})),
            E(name="Romanian deadlift", sets=3, reps=6, load="70kg")])]),
    ])


# Football: sprint intervals and small-sided games written in prose.
FOOTBALL = """Pre-season Football

Week 1
Tuesday - Speed
Sprints: 6 x 30m, 90s rest
Small-sided games: 4 x 4 min, 2 min rest

Week 2
Tuesday - Speed
Sprints: 8 x 30m, 60s rest
Small-sided games: 5 x 4 min, 2 min rest
"""


def football(w2_sprint=None):
    return ReadPlan(name="Pre-season Football", weeks=[
        W(week_index=1, sessions=[S(name="Speed", days=["Tuesday"], exercises=[
            E(name="Sprints", sets=6, distance=30, distance_unit="m", rest_seconds=90),
            E(name="Small-sided games", rounds=4, work_seconds=240, rest_seconds=120)])]),
        W(week_index=2, sessions=[S(name="Speed", days=["Tuesday"], exercises=[
            E(name="Sprints", **(w2_sprint or {"sets": 8, "distance": 30, "distance_unit": "m", "rest_seconds": 60})),
            E(name="Small-sided games", rounds=5, work_seconds=240, rest_seconds=120)])]),
    ])


# Athletics: track session, dated, same rep name twice in one session.
ATHLETICS = """Track - 800m group

Week 1
Wed 25 Nov - Track
400m reps: 6 x 400m, 2 min rest
400m reps: 2 x 400m, 4 min rest
"""


def athletics(second=None):
    return ReadPlan(name="Track - 800m group", weeks=[W(week_index=1, sessions=[S(name="Track", days=["Wednesday"], exercises=[
        E(name="400m reps", sets=6, distance=400, distance_unit="m", rest_seconds=120),
        E(name="400m reps", **(second or {"sets": 2, "distance": 400, "distance_unit": "m", "rest_seconds": 240})),
    ])])])


# Endurance: cycling durations and heart-rate zones across two sessions.
ENDURANCE = """Base Cycling

Week 1
Saturday - Long ride
Endurance ride: 90 min Zone 2
Sunday - Recovery spin
Recovery ride: 40 min Zone 1
"""


def endurance(sunday=None):
    return ReadPlan(name="Base Cycling", weeks=[W(week_index=1, sessions=[
        S(name="Long ride", days=["Saturday"], exercises=[E(name="Endurance ride", seconds=5400, load="Zone 2")]),
        S(name="Recovery spin", days=["Sunday"], exercises=[
            E(name="Recovery ride", **(sunday or {"seconds": 2400, "load": "Zone 1"}))]),
    ])])


# Rehabilitation: per-side holds, tempo, an optional band and RPE.
REHAB = """Knee Rehab Phase 1

Week 1
Monday - Rehab
A1. Spanish squat iso: 5 x 45s holds
A2. Step-down: 3 x 10 each leg, tempo 3-1-1-0
B1. Terminal knee extension: 3 x 15, light band if able, RPE 6
"""


def rehab(**override):
    exercises = [
        E(name="A1. Spanish squat iso", sets=5, seconds=45),
        E(name="A2. Step-down", sets=3, reps=10, per_side=True, tempo="3-1-1-0"),
        E(name="B1. Terminal knee extension", sets=3, reps=15, load="light band if able", rpe=6),
    ]
    for key, value in override.items():
        setattr(exercises[1], key, value)
    return ReadPlan(name="Knee Rehab Phase 1", weeks=[W(week_index=1, sessions=[S(name="Rehab", days=["Monday"], exercises=exercises)])])


# Mobility: minutes written as "m" on time-only work, and per-side holds.
MOBILITY = """Mobility Flow

Week 1
Thursday - Mobility
Couch stretch: 2 x 60s per side
Breathing: 5 minutes
"""


def mobility(breathing=None):
    return ReadPlan(name="Mobility Flow", weeks=[W(week_index=1, sessions=[S(name="Mobility", days=["Thursday"], exercises=[
        E(name="Couch stretch", sets=2, seconds=60, per_side=True),
        E(name="Breathing", **(breathing or {"seconds": 300})),
    ])])])


# MMA / hybrid: wrestling rounds next to a barbell lift with the same notation shape.
HYBRID = """Hybrid Fight Prep

Week 1
Friday - Mat + Lift
Wrestling drills: 5 x 3 min, 1 min rest
Deadlift: 5 x 3 @ 80%
"""


def hybrid(deadlift=None):
    return ReadPlan(name="Hybrid Fight Prep", weeks=[W(week_index=1, sessions=[S(name="Mat + Lift", days=["Friday"], exercises=[
        E(name="Wrestling drills", rounds=5, work_seconds=180, rest_seconds=60),
        E(name="Deadlift", **(deadlift or {"sets": 5, "reps": 3, "load": "80%"})),
    ])])])


PROGRAMMES = {
    "strength": (STRENGTH, strength), "football": (FOOTBALL, football), "athletics": (ATHLETICS, athletics),
    "endurance": (ENDURANCE, endurance), "rehab": (REHAB, rehab), "mobility": (MOBILITY, mobility), "hybrid": (HYBRID, hybrid),
}


@pytest.mark.parametrize("sport", PROGRAMMES)
def test_a_faithful_reading_passes_in_every_discipline(sport):
    source, reading = PROGRAMMES[sport]
    result = build_draft(reading(), START, source)
    assert dose_flags(result) == [], [f.message for f in result.flags]
    names = [e["name"] for s in result.plan["sessions"] for e in s["exercises"]]
    assert len(names) == sum(len(s.exercises) for w in reading().weeks for s in w.sessions), "no exercise is lost"


def exercise_at(result, session, index):
    return [s for s in result.plan["sessions"] if s["exercises"]][session]["exercises"][index]


@pytest.mark.parametrize("sport,bad,session,index", [
    # Week 1's values copied into week 2's repeat of the same exercise.
    ("strength", strength({"sets": 5, "reps": 5, "load": "75%"}), 1, 0),
    ("football", football({"sets": 6, "distance": 30, "distance_unit": "m", "rest_seconds": 90}), 1, 0),
    # The first occurrence's rest given to the second one in the same session.
    ("athletics", athletics({"sets": 2, "distance": 400, "distance_unit": "m", "rest_seconds": 120}), 0, 1),
    # Saturday's ride carried over to Sunday.
    ("endurance", endurance({"seconds": 5400, "load": "Zone 2"}), 1, 0),
    # Invented values that are not written anywhere.
    ("rehab", rehab(reps=12), 0, 1),
    ("mobility", mobility({"seconds": 600}), 0, 1),
    # The wrestling rounds read onto the lift.
    ("hybrid", hybrid({"sets": 5, "reps": 3, "load": "1 min rest"}), 0, 1),
])
def test_borrowed_or_invented_values_are_flagged_on_that_exercise(sport, bad, session, index):
    source, faithful = PROGRAMMES[sport]
    result = build_draft(bad, START, source)
    row = exercise_at(result, session, index)
    truth = exercise_at(build_draft(faithful(), START, source), session, index)
    # Either the written value is restored from its own source line, or the row is flagged.
    assert row["dose"] == truth["dose"] or dose_flags(result, row["id"]), row["dose"]


@pytest.mark.parametrize("source,name,item", [
    # "5 x 3" is sets x reps on a lift, rounds x minutes when a time unit is written.
    ("Deadlift: 5 x 3", "Deadlift", {"sets": 5, "reps": 3}),
    ("Wrestling: 5 x 3 min", "Wrestling", {"rounds": 5, "work_seconds": 180}),
    ("Hill sprints: 5 x 30s", "Hill sprints", {"rounds": 5, "work_seconds": 30}),
    ("Rows: 5 x 300m", "Rows", {"sets": 5, "distance": 300, "distance_unit": "m"}),
])
def test_the_same_notation_reads_by_its_written_unit_not_the_sport(source, name, item):
    reading = ReadPlan(weeks=[W(week_index=1, sessions=[S(name="Session", days=["Monday"], exercises=[E(name=name, **item)])])])
    result = build_draft(reading, START, f"Week 1\nMonday - Session\n{source}\n")
    assert result.flags == [], [f.message for f in result.flags]


@pytest.mark.parametrize("source,name,item", [
    ("Deadlift: 5 x 3", "Deadlift", {"rounds": 5, "work_seconds": 180}),
    ("Wrestling: 5 x 3 min", "Wrestling", {"sets": 5, "reps": 3}),
    ("Rows: 5 x 300m", "Rows", {"rounds": 5, "work_seconds": 300}),
])
def test_the_wrong_reading_of_shared_notation_is_flagged(source, name, item):
    reading = ReadPlan(weeks=[W(week_index=1, sessions=[S(name="Session", days=["Monday"], exercises=[E(name=name, **item)])])])
    result = build_draft(reading, START, f"Week 1\nMonday - Session\n{source}\n")
    assert dose_flags(result), "a misread unit must be reviewed"


@pytest.mark.parametrize("name", ["Sled push", "Farmer carry", "Prowler", "Unknown drill"])
def test_ambiguous_m_on_an_unfamiliar_exercise_is_never_silently_minutes(name):
    reading = ReadPlan(weeks=[W(week_index=1, sessions=[S(name="Session", days=["Monday"], exercises=[E(name=name, seconds=1200)])])])
    result = build_draft(reading, START, f"Week 1\nMonday - Session\n{name}: 20m\n")
    assert dose_flags(result)


def test_overview_rows_rest_days_and_labelled_sections_are_not_missing():
    source = ("Day | Focus\nMon | Strength\nSun | Full rest\n"
              "Mon - Strength\n**Hand Gripper (10 mins)**: 3 sets x 15 reps\n**Mobility (15 mins)**: Follow the hip routine\n")
    reading = ReadPlan(weeks=[W(sessions=[S(name="Strength", days=["Monday"], exercises=[
        E(name="Hand Gripper", sets=3, reps=15), E(name="Hip routine", prescription="Within Mobility (15 mins)")])])])
    result = build_draft(reading, START, source)
    assert not [f.message for f in result.flags if "missing" in f.message.lower()]


def test_an_omitted_optional_session_is_still_flagged():
    source = "Mon - Strength\nBack squat: 3 x 5\nSat - Easy swim (Optional)\nSwim: 20 min\n"
    reading = ReadPlan(weeks=[W(sessions=[S(name="Strength", days=["Monday"], exercises=[E(name="Back squat", sets=3, reps=5)])])])
    result = build_draft(reading, START, source)
    assert any("missing session" in f.message and "Easy swim" in f.message for f in result.flags)


def test_the_same_session_name_on_another_day_is_not_a_duplicate():
    source = "Week 1\nMonday - Strength\nSquat: 3 x 5\n\nFriday - Strength\nDeadlift: 4 x 3\n"
    reading = ReadPlan(weeks=[W(week_index=1, sessions=[S(name="Strength", days=["Monday"], exercises=[
        E(name="Squat", sets=3, reps=5)])])])
    result = build_draft(reading, START, source)
    assert any("missing session" in f.message and "Friday" in f.message for f in result.flags), \
        [f.message for f in result.flags]


def test_a_real_missing_session_is_still_flagged():
    source = "Mon - Strength\nBack squat: 3 x 5\nWed - Conditioning\nBike: 10 x 30s\n"
    reading = ReadPlan(weeks=[W(sessions=[S(name="Strength", days=["Monday"], exercises=[E(name="Back squat", sets=3, reps=5)])])])
    result = build_draft(reading, START, source)
    assert any("missing session" in f.message for f in result.flags)


def test_same_day_different_names_are_distinct_sessions():
    source = "Week 1\nMonday - Strength\nSquat 3x5\nMonday - Conditioning\nBike 10x30s"
    read = ReadPlan(weeks=[W(week_index=1, sessions=[
        S(name="Strength", days=["Monday"], exercises=[E(name="Squat", sets=3, reps=5)])])])
    result = build_draft(read, START, source)
    assert any("Possible missing session: Monday - Conditioning" in f.message for f in result.flags)


def test_same_name_cannot_borrow_the_other_day():
    source = "Week 1\nMonday - Strength\nSquat 3x5\nFriday - Strength\nDeadlift 4x3"
    read = ReadPlan(weeks=[W(week_index=1, sessions=[
        S(name="Strength", days=["Friday"], exercises=[E(name="Deadlift", sets=4, reps=3)])])])
    result = build_draft(read, START, source)
    assert any("Possible missing session: Monday - Strength" in f.message for f in result.flags)


def test_unnamed_heading_can_still_match_its_explicit_weekday():
    source = "Week 1\nMonday\nSquat 3x5"
    read = ReadPlan(weeks=[W(week_index=1, sessions=[
        S(name="Strength", days=["Monday"], exercises=[E(name="Squat", sets=3, reps=5)])])])
    result = build_draft(read, START, source)
    assert not any("Possible missing session" in f.message for f in result.flags)
