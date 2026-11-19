"""Sequential daily programmes ("30-Day Boxer Challenge") import as consecutive
calendar days; weekly, recurring and dated programmes keep their schedules."""
from datetime import date, timedelta

from api.services.plan_import import build_draft
from api.services.plan_import_reading import ReadExercise, ReadPlan, ReadSession, ReadWeek

START = date(2026, 11, 30)  # Monday, as the importer sends by default

# The 30-Day Boxer Challenge as the reader returns it from the screenshot:
# thirty numbered days, no weekdays, no dates.
BOXER_WORKOUTS = [
    ("Shadow Boxing", ReadExercise(name="Shadow boxing", rounds=3, work_seconds=180, rest_seconds=60)),
    ("Jump Rope", ReadExercise(name="Jump rope", rounds=5, work_seconds=120, rest_seconds=30)),
    ("Core", ReadExercise(name="Sit-ups", sets=3, reps=20)),
    ("Heavy Bag", ReadExercise(name="Heavy bag", rounds=4, work_seconds=180, rest_seconds=60)),
    ("Push-ups", ReadExercise(name="Push-ups", sets=4, reps=15)),
    ("Roadwork", ReadExercise(name="Run", distance=3, distance_unit="km")),
    ("Recovery", ReadExercise(name="Stretching", seconds=600)),
]


def boxer_day(n: int) -> ReadSession:
    title, exercise = BOXER_WORKOUTS[(n - 1) % 7]
    return ReadSession(name=f"Day {n} - {title}", session_type="skill", exercises=[exercise])


def boxer_challenge(days=range(1, 31), split_weeks=False) -> ReadPlan:
    sessions = [boxer_day(n) for n in days]
    if split_weeks:  # the reader may also group the days seven to a week
        weeks = [ReadWeek(week_index=i // 7 + 1, sessions=sessions[i:i + 7]) for i in range(0, len(sessions), 7)]
    else:
        weeks = [ReadWeek(sessions=sessions)]
    return ReadPlan(name="30-Day Boxer Challenge", weeks=weeks)


def by_day(result) -> dict[int, date]:
    return {int(s["name"].split()[1]): date.fromisoformat(s["scheduled_date"]) for s in result.plan["sessions"]}


def test_thirty_day_challenge_imports_every_day_on_consecutive_dates():
    for split in (False, True):
        result = build_draft(boxer_challenge(split_weeks=split), START, "")
        assert result.sessions == 30 and result.needs_day == []
        assert result.plan["schedule_mode"] == "dated"
        dates = by_day(result)
        assert sorted(dates) == list(range(1, 31))
        assert all(dates[n] == START + timedelta(days=n - 1) for n in dates)
        assert len(set(dates.values())) == 30, "no two days share a date"
        assert dates[7] == date(2026, 12, 6) and dates[8] == date(2026, 12, 7) and dates[14] == date(2026, 12, 13)
        assert dates[3] != dates[10] and dates[7] != dates[14]
        assert result.plan["start_date"] == "2026-11-30" and result.plan["end_date"] == "2026-12-29"
        assert not any("sessions" in f.message and "Only the first" in f.message for f in result.flags)
        # Exercises and doses come through.
        day4 = next(s for s in result.plan["sessions"] if s["name"].startswith("Day 4 "))
        assert day4["exercises"][0]["name"] == "Heavy bag"
        assert result.exercises == 30


def test_day_one_is_the_selected_start_date_even_midweek():
    result = build_draft(boxer_challenge(), date(2026, 12, 2), "")  # a Wednesday
    dates = by_day(result)
    assert dates[1] == date(2026, 12, 2) and dates[8] == date(2026, 12, 9) and dates[30] == date(2026, 12, 31)


def test_incomplete_extraction_reports_the_missing_days():
    read = [n for n in range(1, 31) if n not in (9, 10, 22)]
    result = build_draft(boxer_challenge(days=read), START, "")
    assert result.sessions == 27
    assert any("Read 27 of 30 days" in f.message and "9-10, 22" in f.message for f in result.flags)
    assert by_day(result)[11] == START + timedelta(days=10), "the days read keep their own dates"


def test_three_week_monday_to_sunday_programme_keeps_its_weeks():
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    weeks = [ReadWeek(week_index=w, sessions=[ReadSession(name=f"W{w} {d}", days=[d], exercises=[ReadExercise(name="Run")])
                                              for d in weekdays]) for w in (1, 2, 3)]
    result = build_draft(ReadPlan(weeks=weeks), START, "")
    dates = [s["scheduled_date"] for s in result.plan["sessions"]]
    assert len(dates) == 21 and len(set(dates)) == 21 and result.needs_day == []
    assert sorted(dates)[0] == "2026-11-30" and sorted(dates)[-1] == "2026-12-20"


def test_recurring_tuesday_thursday_programme_stays_on_its_weekdays():
    read = ReadPlan(weeks=[ReadWeek(repeat=4, sessions=[
        ReadSession(name="Boxing", days=["Tuesday", "Thursday"], exercises=[ReadExercise(name="Pads")])])])
    result = build_draft(read, START, "")
    dates = sorted(date.fromisoformat(s["scheduled_date"]) for s in result.plan["sessions"])
    assert len(dates) == 8 and {d.strftime("%A") for d in dates} == {"Tuesday", "Thursday"}
    assert result.needs_day == []


def test_explicitly_dated_programme_keeps_its_dates():
    read = ReadPlan(weeks=[ReadWeek(sessions=[
        ReadSession(name="Upper Strength", date="2026-12-01", exercises=[ReadExercise(name="Bench", sets=3, reps=5)]),
        ReadSession(name="Power", date="2026-12-03", exercises=[ReadExercise(name="Jumps", sets=3, reps=5)])])])
    result = build_draft(read, START, "")
    assert sorted(s["scheduled_date"] for s in result.plan["sessions"]) == ["2026-12-01", "2026-12-03"]


def test_short_numbered_weekly_template_still_asks_for_days():
    # Day 1-3 with no weekdays is a weekly template, as before.
    read = ReadPlan(weeks=[ReadWeek(sessions=[boxer_day(n) for n in (1, 2, 3)])])
    result = build_draft(read, START, "")
    assert result.plan["schedule_mode"] == "weekly" and len(result.needs_day) == 3


def test_numbered_days_restarting_each_week_stay_weekly_templates():
    # "Week 2 · Day 1" repeats Day 1: not a sequential challenge.
    weeks = [ReadWeek(week_index=w, sessions=[boxer_day(n) for n in (1, 2, 3)]) for w in (1, 2)]
    result = build_draft(ReadPlan(weeks=weeks), START, "")
    assert result.plan["schedule_mode"] == "dated" and sum(len(g.session_ids) for g in result.needs_day) == 6


def test_missing_final_days_are_reported_from_the_stated_length():
    # The reader stopped after Day 14 of a "30-Day Boxer Challenge".
    result = build_draft(boxer_challenge(days=range(1, 15)), START, "")
    assert result.sessions == 14
    assert any("Read 14 of 30 days" in f.message and "15-30" in f.message for f in result.flags)


def test_days_all_beyond_the_plan_limit_are_refused_not_crashing():
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as raised:
        build_draft(boxer_challenge(days=range(85, 95)), START, "")
    assert raised.value.status_code == 422
