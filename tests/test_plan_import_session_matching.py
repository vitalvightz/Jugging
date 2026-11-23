"""Which source session headings count as read, and which are flagged as possibly missing.

A heading whose title isn't a session's title can still be that session: an overview
row and its full section share the programme's "Day N" label, and a one-line day
("Tue: Flow Roll 3 x 5 min") is written as its exercise. Neither may hide a different
session that shares only a day label or an exercise name.
"""
from datetime import date

from api.services.plan_import import build_draft
from api.services.plan_import_reading import ReadExercise, ReadPlan, ReadSession, ReadWeek

MONDAY = date.fromisoformat("2026-11-23")


def _missing(source: str, *sessions: ReadSession) -> list[str]:
    draft = build_draft(ReadPlan(weeks=[ReadWeek(sessions=list(sessions))]), MONDAY, source)
    prefix = "Possible missing session: "
    return [f.message[len(prefix):].removesuffix(". Compare with the source.")
            for f in draft.flags if f.message.startswith(prefix)]


def _session(name: str, day: str | None, *exercises: ReadExercise) -> ReadSession:
    return ReadSession(day=day, name=name, session_type="mixed", exercises=list(exercises))


SQUAT = ReadExercise(name="Back squat", sets=5, reps=5)
FLOW_ROLL = ReadExercise(name="Grapplers Flow Roll", rounds=3, work_seconds=300, rest_seconds=60)
MOBILITY = ReadExercise(name="Ankle mobility", seconds=600)


# A different session that shares only a day label or an exercise is still flagged.

def test_a_different_session_with_the_same_day_label_is_still_flagged():
    source = "Day 1 AM: Conditioning\nBike intervals 6 x 1 min\nDay 1 PM: Strength\nBack squat 5 x 5"
    read = _session("Day 1 AM: Conditioning", None, ReadExercise(name="Bike intervals", rounds=6, work_seconds=60))
    assert _missing(source, read) == ["Day 1 PM: Strength"]


def test_a_different_session_with_the_same_day_label_and_weekday_is_still_flagged():
    source = "Mon (Day 1) | Conditioning\nMon (Day 1) | Strength\nDay 1 – Conditioning\nBike intervals 6 x 1 min"
    read = _session("Day 1 – Conditioning", "Monday", ReadExercise(name="Bike intervals", rounds=6, work_seconds=60))
    assert _missing(source, read) == ["Mon (Day 1) | Strength"]


def test_a_different_session_that_shares_an_exercise_name_is_still_flagged():
    source = "Mon: Strength - Back squat 5 x 5\nMon: Conditioning - Back squat jumps 3 x 20"
    assert _missing(source, _session("Strength", "Monday", SQUAT)) == ["Mon: Conditioning - Back squat jumps 3 x 20"]


def test_a_one_line_day_is_not_matched_to_another_days_session_with_that_exercise():
    source = "Mon: Back squat 5 x 5\nWed: Back squat 3 x 3"
    assert _missing(source, _session("Lower", "Monday", SQUAT)) == ["Wed: Back squat 3 x 3"]


def test_one_session_satisfies_one_line_day_once():
    source = "Mon: Back squat 5 x 5\nMon: Back squat 3 x 3"
    assert _missing(source, _session("Lower", "Monday", SQUAT)) == ["Mon: Back squat 3 x 3"]


# The same session written under another title is matched, not flagged.

def test_a_one_line_day_written_as_its_exercise_is_matched():
    source = "Tue: Grapplers Flow Roll 3 x 5 min, 1 min rest"
    assert _missing(source, _session("Skill", "Tuesday", FLOW_ROLL)) == []


def test_one_line_days_each_match_their_own_session():
    source = "Mon: Back squat 5 x 5\nTue: Grapplers Flow Roll 3 x 5 min, 1 min rest"
    assert _missing(source, _session("Lower", "Monday", SQUAT), _session("Skill", "Tuesday", FLOW_ROLL)) == []


def test_an_overview_row_and_its_full_section_share_the_day_label():
    full = "Day 5 – Combo Day: Mobility + Core + Boxing"
    read = _session(full, "Friday", MOBILITY)
    overview_first = f"Fri (Day 5) | Mixed Mobility + Shadowboxing + Core\n{full}\nAnkle mobility 10 min"
    full_first = f"{full}\nAnkle mobility 10 min\nFri (Day 5) | Mixed Mobility + Shadowboxing + Core"
    assert _missing(overview_first, read) == []
    assert _missing(full_first, read) == []


def test_a_bare_day_label_matches_its_session():
    source = "Day 2\nBack squat 5 x 5"
    assert _missing(source, _session("Day 2 – Lower", "Tuesday", SQUAT)) == []
