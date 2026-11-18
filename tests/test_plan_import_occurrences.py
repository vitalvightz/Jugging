"""Source-fidelity tests. Readings are deterministic, hand-authored model doubles.

These exercise the real checker/build/save/publication path, not live AI extraction.
The expected doses below are independent of the source matching implementation.
"""
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

import pytest

from api.services import plan_import
from api.services.plan_import import build_draft
from api.services.plan_import_check import read_dose_line, week_heading
from api.services.plan_import_patch import DOSE_FIELDS, apply_patch
from api.services.plan_import_reading import ReadExercise, ReadPlan, ReadSession, ReadWeek, _days
from tests.test_plan_import import api, AUTH, FakeReader, use  # noqa: F401

SOURCE = (Path(__file__).parent / "fixtures/plan_import/boxer_2026_three_weeks.txt").read_text(encoding="utf-8")
START = date(2026, 11, 18)


def reps(sets, count, load=None, side=None):
    return {"mode": "reps", "sets": sets, "reps": count,
            **({"load_or_intensity": load} if load else {}), **({"per_side": True} if side else {})}


def text(value, load=None, side=None):
    return {"mode": "text", "prescription": value, **({"load_or_intensity": load} if load else {}),
            **({"per_side": True} if side else {})}


def timed(seconds, sets=None, side=None):
    return {"mode": "duration", "duration_seconds": seconds, **({"sets": sets} if sets else {}),
            **({"per_side": True} if side else {})}


def intervals(rounds, seconds, rest=None):
    return {"mode": "intervals", "rounds": rounds, "work_seconds": seconds,
            **({"rest_seconds": rest} if rest is not None else {})}


def truth():
    """Every session and exercise, including authored open work, warmups and cooldowns."""
    weeks = []
    for w in range(3):
        upper = [
            ("Push-ups", text(["4x8-10", "4x10-12", "4x8-10"][w], ["add 10kg plate if able", "add 15kg plate", "add 15-20kg plate"][w])),
            ("DB rows", [text("4x8-10", "24-28kg", True), reps(4, 10, "26-30kg", True), reps(4, 8, "30-34kg", True)][w]),
            ("Strict press", text(["3x6-8", "3x7-8", "3x5-6"][w], ["40-45kg", "42-47kg", "45-50kg"][w])),
            ("Face pulls", reps(3, 15, ["15-20kg", "20kg", "20kg"][w])),
            ("Chin-ups", text(["3x6-10", "3x7-10", "3x6-8"][w], ["add 5kg if able", "add 7.5kg", "add 10kg"][w])),
            ("Band external rotations", reps(3, 12, side=True)),
            ("Plank", timed([45, 50, 45][w], 3)),
            ("Side plank", timed([30, 35, 30][w], 2, True)),
        ]
        if w == 0:
            upper = [("Warm-up", timed(600)), ("band pull-aparts", reps(2, 15)),
                     ("scap push-ups", reps(2, 10)), ("skip rope", timed(180))] + upper + [("Stretch", timed(300))]
        road = [("Easy run", text("30-40 min") if w < 2 else timed(1800)),
                (["Boxing session", "Technical/pads session", "Technical session"][w], text(["technical, moderate", "Technical/pads session", "sharp and light"][w])),
                ("Shadowbox", intervals([3, 3, 2][w], 180, 60 if w == 0 else None))]
        power = [
            ("Deadlift", reps([5, 5, 4][w], [3, 3, 2][w], ["100-110kg", "110-120kg", "115-125kg"][w])),
            ("Med ball rotational throws", reps(3, [5, 6, 5][w], ["6-8kg", "8kg", "8kg"][w], True)),
            ("Box jumps", reps(3, [5, 5, 4][w])),
            ("DB push press", reps(3, [5, 6, 5][w], ["20-24kg", "22-26kg", "24-28kg"][w], True)),
            ("Woodchops", reps(3, [10, 12, 10][w], ["20-25kg", "25kg", "25kg"][w], True)),
            ("Sled push / hill sprints", text("Sled push 5x20m @ 40-60kg / hill sprints 5x20m")) if w == 0 else
            ("Hill sprints", {"mode": "distance", "sets": [5, 6, 5][w], "distance": 20.0, "distance_unit": "m"}),
            ("Hanging knee raises", reps(3, [10, 12, 10][w])),
        ]
        if w == 0:
            power = [("Warm-up", timed(600)), ("med ball slams", reps(2, 8, "8kg"))] + power
        sparring = [("Sparring/technical rounds", text("as scheduled")),
                    ("Finisher", intervals([6, 8, 5][w], 30, 30)), ("walk + stretch", timed(600))]
        recovery = [("Easy walk/cycle", text("30-40 min")), ("Mobility", timed(900))]
        if w != 1:
            recovery.append(("Foam roll", text("Foam roll")))
        lower = [
            ("Back squat", [text("4x6-8", "80-90kg"), text("4x6-8", "85-95kg"), reps(3, 5, "90-100kg")][w]),
            ("RDL", [reps(3, 8, "70-80kg"), text("3x8-10", "75-85kg"), reps(3, 6, "80-90kg")][w]),
            ("Split squats", reps(3, [8, 10, 8][w], ["20-24kg", "22-26kg", "24-28kg"][w], True)),
            ("Calf raises", reps(3, 15, ["40kg", "45kg", "45kg"][w])),
            ("Pallof press", reps(3, 12, ["15-20kg", "20kg", "20kg"][w], True)),
            ("Dead bug", reps(3, [10, 12, 10][w], side=True)),
        ]
        if w == 0:
            lower = [("leg swings", text("leg swings")), ("BW squats", reps(2, 15)), ("glute bridges", reps(2, 15))] + lower
        tuesday = [("Easy run", text(["35-45 min (or 20 min tempo)", "35-45 min", "30 min"][w])),
                   ("Skill session", text(["footwork + combos", "Skill session", "light"][w])),
                   ("Skip rope", intervals([4, 5, 3][w], 120, 30))]
        weeks.append(list(zip(["Upper Strength", "Roadwork + Boxing", "Power", "Sparring + Conditioning", "Recovery", "Lower Strength", "Roadwork + Boxing"],
                              [upper, road, power, sparring, recovery, lower, tuesday])))
    return weeks


def reading():
    # Ideal extraction constructed from the independently hand-authored truth.
    # Short source dates intentionally require programme-week month context.
    weeks = []
    for wi, sessions in enumerate(truth()):
        built = []
        for si, (title, exercises) in enumerate(sessions):
            when = START + timedelta(days=wi * 7 + si)
            rows = []
            for name, dose in exercises:
                values = {({"duration_seconds": "seconds", "load_or_intensity": "load"}.get(k, k)): v
                          for k, v in dose.items() if k != "mode"}
                rows.append(ReadExercise(name=name, **values))
            kind = ["strength_power", "skill", "strength_power", "skill", "recovery", "strength_power", "skill"][si]
            built.append(ReadSession(name=title, days=[when.strftime("%A")], date=f"{when:%a} {when.day}",
                                     session_type=kind, exercises=rows))
        weeks.append(ReadWeek(week_index=wi + 1, sessions=built))
    return ReadPlan(name="Boxer S&C", start_date=str(START), weeks=weeks)


def compact(dose):
    return {k: v for k, v in dose.items() if v is not None}


def assert_truth(plan):
    assert len(plan["sessions"]) == 21
    for i, session in enumerate(plan["sessions"]):
        title, exercises = truth()[i // 7][i % 7]
        assert session["scheduled_date"] == str(START + timedelta(days=i))
        assert session["programme_week"] == i // 7 + 1
        assert session["name"] == title
        assert [(e["name"], compact(e["dose"])) for e in session["exercises"]] == exercises


def test_exact_three_week_source_preserves_every_occurrence():
    original = reading()
    before = original.model_dump()
    result = build_draft(original, START, SOURCE)
    assert_truth(result.plan)
    assert not result.needs_day
    assert not result.flags, [(f.where, f.message) for f in result.flags]
    assert original.model_dump() == before, "checking a draft must not mutate the first reading"


@pytest.mark.parametrize("week", range(3))
@pytest.mark.parametrize("session,row", [(0, 0), (0, 1), (0, 2), (2, 0), (5, 0)])
def test_patch_isolates_one_position_with_identical_names(week, session, row):
    read = reading()
    # Isolate the addressing property from source checks, even for identical titles/dates.
    for w in read.weeks:
        for s in w.sessions:
            s.name = "Same session"
            for e in s.exercises:
                e.name = "Same exercise"
                e.notes = "Keep original notes"
                e.cues = ["Keep original cues"]
    before = read.model_dump()
    item = {"id": 0, "name": "Same exercise", "occurrence": (week, session, row)}
    patched = apply_patch(read, [item], [{"id": 0, "load": "corrected"}])
    expected = deepcopy(before)
    expected["weeks"][week]["sessions"][session]["exercises"][row]["load"] = "corrected"
    assert patched.model_dump() == expected
    assert read.model_dump() == before


@pytest.mark.parametrize("occurrence", [None, (), (0, 0), (-2, 0, 0), (0, -1, 0), (0, 0, -1), (3, 0, 0), (0, 99, 0), (0, 0, 99), (True, 0, 0)])
def test_invalid_patch_never_falls_back_to_name(occurrence):
    read = reading()
    items = [{"id": 0, "name": "Push-ups", "session": "Upper Strength", "occurrence": occurrence}]
    assert apply_patch(read, items, [{"id": 0, "load": "wrong"}]).model_dump() == read.model_dump()


@pytest.mark.parametrize("fixes,items", [
    ([{"id": 0, "load": "wrong"}, {"id": 0, "load": "also wrong"}], [{"id": 0, "name": "Push-ups", "occurrence": (0, 0, 4)}]),
    ([{"id": 0, "load": "wrong"}], [{"id": 0, "name": "Push-ups", "occurrence": (0, 0, 4)}, {"id": 1, "name": "Push-ups", "occurrence": [0, 0, 4]}]),
    ([{"id": 0, "reps": "999"}], [{"id": 0, "name": "Push-ups", "occurrence": (0, 0, 4)}]),
    ([{"id": 0, "load": {"value": 999}}], [{"id": 0, "name": "Push-ups", "occurrence": (0, 0, 4)}]),
])
def test_ambiguous_or_malformed_patches_are_ignored(fixes, items):
    read = reading()
    assert apply_patch(read, items, fixes).model_dump() == read.model_dump()


@pytest.mark.parametrize("heading", ["WEEK 5", "Week 5 — Strength", "W5", "WK 5", "Week Five", "Fifth Week", "Microcycle 5", "Phase 5: Strength"])
def test_week_heading_variations_preserve_number(heading):
    assert week_heading(heading) == {5}


@pytest.mark.parametrize("heading", ["add 1 rep", "Use W1 weights", "Week 3 is lighter by design", "W1 sprint 5x20m"])
def test_numbers_inside_advice_are_not_week_sections(heading):
    assert not week_heading(heading)


@pytest.mark.parametrize("notation", ["4x8", "4 x 8", "4×8", "4 sets of 8 reps", "8 reps × 4 sets"])
def test_equivalent_repetition_notation(notation):
    dose = read_dose_line(notation)
    assert dose.roles["count"] == {4}
    assert dose.roles["reps"] == {8}


@pytest.mark.parametrize("notation", ["3x45s", "3 sets × 45 sec", "3 rounds of 45 seconds"])
def test_equivalent_timed_notation(notation):
    dose = read_dose_line(notation)
    assert dose.roles["count"] == {3}
    assert dose.roles["work"] == {45}


@pytest.mark.parametrize("written,expected", [("Tue/Thu", ["Tuesday", "Thursday"]), ("Tues & Thurs", ["Tuesday", "Thursday"]),
    ("Monday/Wed", ["Monday", "Wednesday"]), ("Mon, Wed & Fri", ["Monday", "Wednesday", "Friday"]),
    ("Monday–Friday", ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]), ("Day 1", []), ("Session A", [])])
def test_days_are_explicit(written, expected):
    assert _days(written) == expected


@pytest.mark.parametrize("wi,si,ei,changes", [
    (0, 0, 5, {"load": "30-34kg"}),  # Week 3 row load on Week 1
    (1, 0, 1, {"reps": 8}),
    (0, 0, 0, {"load": "100-110kg"}),  # warmup borrowing a lift's load
    (0, 0, 4, {"load": "10kg"}),  # optional load made mandatory
    (0, 0, 6, {"reps": 8, "prescription": None}),  # collapsed range
    (0, 3, 0, {"rounds": 6, "work_seconds": 180}),  # unprescribed sparring
    (0, 2, 2, {"load": "115-125kg"}),
    (1, 3, 1, {"rounds": 5}),  # unique source value can be corrected deterministically
])
def test_cross_occurrence_and_semantic_corruption_is_not_silently_confirmed(wi, si, ei, changes):
    read = reading()
    exercise = read.weeks[wi].sessions[si].exercises[ei]
    for key, value in changes.items():
        setattr(exercise, key, value)
    result = build_draft(read, START, SOURCE)
    expected = dict(truth()[wi][si][1])[exercise.name]
    actual = result.plan["sessions"][wi * 7 + si]["exercises"][ei]
    assert compact(actual["dose"]) == expected or any(f.exercise_id == actual["id"] for f in result.flags)


def test_missing_week_or_session_is_reviewed_without_borrowing():
    for source in (SOURCE.replace("WEEK 2 — INTENSIFICATION", ""), SOURCE.replace("Fri 27 – Power", "")):
        read = reading()
        read.weeks[1].sessions[2].exercises[0].load = "100-110kg"
        result = build_draft(read, START, source)
        row = result.plan["sessions"][9]["exercises"][0]
        assert row["dose"]["load_or_intensity"] == "100-110kg"
        assert any(f.exercise_id == row["id"] and "Couldn't find" in f.message for f in result.flags)


def test_full_import_review_save_reload_publish_read(api):  # noqa: F811
    client, store, monkeypatch = api
    use(monkeypatch, FakeReader(reading()))
    response = client.post("/api/custom-plans/import", headers=AUTH, json={"text": SOURCE, "start_date": str(START)})
    assert response.status_code == 200, response.text
    result = response.json()
    assert not result["flags"]
    assert_truth(result["plan"])
    saved = client.post("/api/custom-plans", headers=AUTH, json=result["plan"])
    assert saved.status_code == 201, saved.text
    draft = client.get(f"/api/custom-plans/{saved.json()['id']}", headers=AUTH)
    assert draft.status_code == 200, draft.text
    assert_truth(draft.json())
    published = client.post(f"/api/custom-plans/{saved.json()['id']}/open-plan", headers=AUTH)
    assert published.status_code == 200, published.text
    structured = store.plans[published.json()["plan_id"]]["structured_plan"]
    sessions = [(d["date"], s) for w in structured["weeks"] for d in w["days"] for s in d["sessions"]]
    assert len(sessions) == 21
    for i, (when, session) in enumerate(sessions):
        assert when == str(START + timedelta(days=i))
        assert session["programme_week"] == i // 7 + 1
        expected = truth()[i // 7][i % 7][1]
        assert [b["display_name"] for b in session["blocks"]] == [name for name, _ in expected]
        for block, (_, dose) in zip(session["blocks"], expected):
            for key in ("sets", "reps", "rounds"):
                if key in dose:
                    assert block[key] == dose[key]
            if "load_or_intensity" in dose:
                assert dose["load_or_intensity"] in str(block)
            if "prescription" in dose:
                assert dose["prescription"] in str(block)
            for key, field in [("duration_seconds", "duration"), ("work_seconds", "work"), ("rest_seconds", "rest")]:
                if key in dose:
                    assert block[field] == {"value": dose[key], "unit": "seconds"}


def test_horizontal_week_columns_do_not_validate_another_columns_load():
    source = """Session A
Exercise | Week 5 | Week 6 | Week 7
Deadlift | 5x3 @ 100-110kg | 5x3 @ 110-120kg | 4x2 @ 115-125kg
"""
    read = ReadPlan(weeks=[ReadWeek(week_index=week, sessions=[ReadSession(name="Session A", exercises=[
        ReadExercise(name="Deadlift", sets=5, reps=3, load="115-125kg")])]) for week in (5, 6, 7)])
    result = build_draft(read, START, source)
    assert any("load" in f.message for f in result.flags if f.where.startswith("Week 5"))
    assert "115-125kg" not in result._review[0]["line"]
    assert "100-110kg" in result._review[0]["line"]


def test_same_name_twice_in_session_and_two_sessions_on_same_date():
    source = """Week 1
Monday – Lift AM
Squat 3x5 @ 100kg
Squat 2x8 @ 80kg
Monday – Lift PM
Squat 4x3 @ 110kg
"""
    read = ReadPlan(weeks=[ReadWeek(week_index=1, sessions=[
        ReadSession(name="Lift AM", days=["Monday"], exercises=[ReadExercise(name="Squat", sets=3, reps=5, load="110kg"),
            ReadExercise(name="Squat", sets=2, reps=8, load="80kg")]),
        ReadSession(name="Lift PM", days=["Monday"], exercises=[ReadExercise(name="Squat", sets=4, reps=3, load="110kg")]),
    ])])
    result = build_draft(read, START, source)
    assert len(result._review) == 1
    assert result._review[0]["occurrence"] == (0, 0, 0)
    assert "100kg" in result._review[0]["line"]
    assert len(result.plan["sessions"]) == 2


def test_second_look_accepts_only_individually_verified_corrections():
    source = "\n".join(f"Week {w}\nSession A\nSquat 3x5 @ {load}kg" for w, load in [(1, 100), (2, 110), (3, 120)])
    read = ReadPlan(weeks=[ReadWeek(week_index=w, sessions=[ReadSession(name="Session A", exercises=[
        ReadExercise(name="Squat", sets=3, reps=5, load="999kg", notes="Stay braced", cues=["Chest up"])])]) for w in (1, 2, 3)])
    class Reader:
        def patch(self, items, timeout=None):
            return [{"id": item["id"], **item["read"], "load": ["100kg", "120kg", "120kg"][item["occurrence"][0]]} for item in items]
    def build(r):
        return build_draft(r, START, source)
    result = plan_import._second_look(Reader(), read, build(read), build)
    assert [s["exercises"][0]["dose"]["load_or_intensity"] for s in result.plan["sessions"]] == ["100kg", "999kg", "120kg"]
    assert len(result._review) == 1 and result._review[0]["occurrence"] == (1, 0, 0)
    assert all(s["exercises"][0]["coaching_cues"] == ["Chest up"] for s in result.plan["sessions"])


def test_correction_cannot_erase_a_prescription_to_pass_validation():
    source = "Session A\nSquat 3x5 @ 100kg"
    read = ReadPlan(weeks=[ReadWeek(sessions=[ReadSession(name="Session A", exercises=[
        ReadExercise(name="Squat", sets=5, reps=3, load="100kg")])])])
    class Reader:
        def patch(self, items, timeout=None):
            return [{"id": item["id"], **{f: None for f in DOSE_FIELDS}} for item in items]
    def build(r):
        return build_draft(r, START, source)
    first = build(read)
    assert plan_import._second_look(Reader(), read, first, build) is first


def test_corrections_to_originals_are_copied_by_explicit_references():
    source = "Week 1\nDay 1\nSquat 3x5\nWeek 2\nDay 1\nSame as Week 1"
    read = ReadPlan(weeks=[ReadWeek(week_index=1, sessions=[ReadSession(name="Day 1", exercises=[
        ReadExercise(name="Squat", sets=3, reps=9)])]), ReadWeek(week_index=2, sessions=[ReadSession(name="Day 1", exercises=[
        ReadExercise(name="Same as Week 1", refers_to_day="Week 1")])])])
    result = build_draft(read, START, source)
    assert [s["exercises"][0]["dose"]["reps"] for s in result.plan["sessions"]] == [5, 5]


@pytest.mark.parametrize("start,dates,expected", [
    ("2026-11-25", ["Wed 25", "Tue 1 Dec", "Wed 2", "Tue 8"], ["2026-11-25", "2026-12-01", "2026-12-02", "2026-12-08"]),
    ("2026-12-30", ["Wed 30", "Tue 5 Jan", "Wed 6", "Tue 12"], ["2026-12-30", "2027-01-05", "2027-01-06", "2027-01-12"]),
])
def test_programme_week_dates_cross_months_and_years(start, dates, expected):
    read = ReadPlan(start_date=start, weeks=[ReadWeek(week_index=5+w, sessions=[ReadSession(name=label, date=label,
        days=["Wednesday" if i % 2 == 0 else "Tuesday"], exercises=[ReadExercise(name="Run", seconds=600)])
        for i, label in enumerate(dates[w*2:w*2+2])]) for w in range(2)])
    result = build_draft(read, date.fromisoformat(start))
    assert [s["scheduled_date"] for s in result.plan["sessions"]] == expected
    assert [s["programme_week"] for s in result.plan["sessions"]] == [5, 5, 6, 6]


def test_loading_stage_is_checked_by_row_and_percentage_column():
    from api.structured_plan_models import ExercisePrescription
    source = """Week 5
Day 1
Code | Exercise | % | Sets x Reps | WT
A1 | Clean | 50% | 2x2 | 120
 | | 95% | 3x1 | 225
"""
    read = ReadPlan(weeks=[ReadWeek(week_index=5, sessions=[ReadSession(name="Day 1", exercises=[
        ReadExercise(name="Clean", exercise_code="A1", prescribed_sets=[ExercisePrescription(percentage=95, sets=2, reps=2, source_text="WT: 120")]),
        ReadExercise(name="", prescribed_sets=[ExercisePrescription(percentage=95, sets=3, reps=1, source_text="WT: 225")])])])])
    result = build_draft(read, START, source)
    assert any("loading stage" in f.message for f in result.flags)
    assert len(result.plan["sessions"][0]["exercises"]) == 1
    assert all(f.exercise_id == result.plan["sessions"][0]["exercises"][0]["id"] for f in result.flags if "loading stage" in f.message)


def test_short_sprint_table_progression_preserves_later_weeks_reductions():
    """Clearly legible cells from Photo 1; ambiguous cells are deliberately excluded.

    Cycles 8-11, Day 2 RDL: 70%/8/115, 72%/8/120, 80%/6/130,
    80%/4/130. Day 1 bench: 3x6, 3x6, 3x6, 1x6.
    """
    from api.structured_plan_models import ExercisePrescription
    source = """Day 1
Exercise | Week 8 | Week 9 | Week 10 | Week 11
DB Bench Press | 3x6 | 3x6 | 3x6 | 1x6
Day 2
Exercise | Week 8 | Week 9 | Week 10 | Week 11
Barbell RDL | 8 @ 70%, WT 115 | 8 @ 72%, WT 120 | 6 @ 80%, WT 130 | 4 @ 80%, WT 130
"""
    read = ReadPlan(weeks=[ReadWeek(week_index=8+w, sessions=[
        ReadSession(name="Day 1", exercises=[ReadExercise(name="DB Bench Press", sets=[3,3,3,1][w], reps=6)]),
        ReadSession(name="Day 2", exercises=[ReadExercise(name="Barbell RDL", prescribed_sets=[
            ExercisePrescription(reps=[8,8,6,4][w], percentage=[70,72,80,80][w], source_text=f"WT: {[115,120,130,130][w]}")])]),
    ]) for w in range(4)])
    result = build_draft(read, START, source)
    # Nonstandard combined table cells require review; prescriptions must survive regardless.
    assert [result.plan["sessions"][w*2]["exercises"][0]["dose"]["sets"] for w in range(4)] == [3,3,3,1]
    assert [result.plan["sessions"][w*2+1]["exercises"][0]["dose"]["prescribed_sets"][0]["reps"] for w in range(4)] == [8,8,6,4]


@pytest.mark.parametrize("name", ["DB Row", "DB rows", "Dumbbell rows", "Dumbbell Row", "Single-arm DB row"])
@pytest.mark.parametrize("prefix", ["", "A1. ", "A1) ", "A1: ", "☐ ", "• ", "- ", "1. "])
def test_exercise_prefixes_and_variations_stay_inside_the_session(name, prefix):
    source = f"Week 5\nSession A\n{prefix}{name} 4x8 @ 24-28kg"
    read = ReadPlan(weeks=[ReadWeek(week_index=5, sessions=[ReadSession(name="Session A", exercises=[
        ReadExercise(name="DB rows", sets=4, reps=8, load="24-28kg")])])])
    result = build_draft(read, START, source)
    assert not result.flags, [(f.where, f.message) for f in result.flags]


@pytest.mark.parametrize("written,field,value", [
    ("4x8-10", "rep_ranges", ["8-10"]), ("4 × 8–10", "rep_ranges", ["8-10"]),
    ("5x20m", "distances", {(20.0, "m")}), ("80–90 kg", "weights", {(80.0, "kg"), (90.0, "kg")}),
    ("70% 1RM", "weights", {(70.0, "%")}),
])
def test_ranges_units_and_distances_are_preserved(written, field, value):
    assert getattr(read_dose_line(written), field) == value


@pytest.mark.parametrize("written,expected", [("30s on / 30s off", ({30}, {30})), ("6 rounds of 3 min, 1 min rest", ({180}, {60}))])
def test_work_and_rest_are_distinct(written, expected):
    dose = read_dose_line(written)
    assert (dose.roles["work"], dose.roles["rest"]) == expected


@pytest.mark.parametrize("when", ["Wed 18", "Wednesday 18 November", "18 Nov 2026", "18/11/2026", "2026-11-18"])
def test_single_source_week_with_written_dates_keeps_its_date(when):
    read = ReadPlan(start_date=str(START), weeks=[ReadWeek(week_index=5, sessions=[ReadSession(name="Session A", date=when,
        days=["Wednesday"], exercises=[ReadExercise(name="Run", seconds=600)])])])
    result = build_draft(read, START)
    assert result.plan["schedule_mode"] == "dated"
    assert result.plan["sessions"][0]["scheduled_date"] == str(START)
    assert result.plan["sessions"][0]["programme_week"] == 5


def test_second_look_cannot_hide_a_missing_load_behind_unrelated_notes():
    source = "Session A\nSquat 3x5 @ 100kg"
    read = ReadPlan(weeks=[ReadWeek(sessions=[ReadSession(name="Session A", exercises=[
        ReadExercise(name="Squat", sets=3, reps=5, load="999kg", notes="Stay braced")])])])
    class Reader:
        def patch(self, items, timeout=None):
            return [{"id": item["id"], **item["read"], "load": None} for item in items]
    def build(r):
        return build_draft(r, START, source)
    first = build(read)
    assert plan_import._second_look(Reader(), read, first, build) is first


def test_empty_later_sessions_are_explicitly_flagged_for_screenshot_review():
    read = ReadPlan(weeks=[ReadWeek(sessions=[ReadSession(name="Day 1", session_type="strength_power", exercises=[
        ReadExercise(name="Clean", sets=3, reps=2)])]), ReadWeek(sessions=[ReadSession(name="Day 1", session_type="strength_power")])])
    result = build_draft(read, START, "")
    assert any("No exercises were read" in f.message and f.where.startswith("Week 2") for f in result.flags)


def test_collapsing_distinct_written_weeks_into_one_template_is_never_confirmed():
    read = reading()
    read.weeks = [read.weeks[0]]
    read.weeks[0].repeat = 3
    result = build_draft(read, START, SOURCE)
    assert any("Separate written weeks" in f.message for f in result.flags)
    assert any("Couldn't find" in f.message for f in result.flags)
    assert [s["scheduled_date"] for s in result.plan["sessions"]] == [str(START + timedelta(days=i)) for i in range(21)]


def test_duplicate_week_numbers_cannot_hide_a_missing_source_week():
    read = reading()
    read.weeks[1].week_index = 1
    result = build_draft(read, START, SOURCE)
    assert any("week numbers 2 are missing" in f.message for f in result.flags)


@pytest.mark.parametrize("line,exercise", [
    ("Squat 4x8 RPE 7-8", ReadExercise(name="Squat", sets=4, reps=8, rpe=8)),
    ("Intervals 6x3 min, 60-90s rest", ReadExercise(name="Intervals", rounds=6, work_seconds=180, rest_seconds=90)),
    ("Run 5-10km", ReadExercise(name="Run", distance=10, distance_unit="km")),
])
def test_all_range_types_keep_both_endpoints(line, exercise):
    read = ReadPlan(weeks=[ReadWeek(sessions=[ReadSession(name="Session A", exercises=[exercise])])])
    result = build_draft(read, START, f"Session A\n{line}")
    assert any("range" in f.message for f in result.flags)
