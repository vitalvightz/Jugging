"""The athlete's three injury-impact choices give one decision everywhere.

* not_limiting: normal training once triage has cleared it (awareness only).
* limiting: exercise-specific adaptation from movement demands and severity.
* cant_train: normal training restricted, rehab/recovery still permitted.

Medical evidence (red flags, serious-injury triage, holds, wounds, "worse"
reports) is independent and always wins over the athlete's impact choice.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from api.contracts.injury_policy import resolve_injury_policy
from api.contracts.readiness_message import (
    ReadinessCheckin, ReadinessContext, build_readiness_adjustment, restricts_training,
)
from fightcamp.conditioning import get_conditioning_bank
from fightcamp.injury_awareness import (
    injury_stop_phrase, is_awareness_only, is_functional_cant_train, reported_training_impact,
)
from fightcamp.injury_guard import injury_decision
from fightcamp.input_parsing import _build_guided_injury, _parse_guided_injury
from fightcamp.rehab_clinical import load_clinical_policies
from fightcamp.rehab_protocols import get_rehab_bank
from fightcamp.strength import get_exercise_bank

SEVERITY = {"not_limiting": "mild", "limiting": "moderate", "cant_train": "severe"}  # web flagSeverity


def _flag(impact=None, *, severity=None, area="Left knee", text="left knee pain", **extra):
    marker = f" [training_impact:{impact}]" if impact else ""
    return {"id": "f1", "status": "open", "body_area": area, "label": area.lower(),
            "description": text + marker, "severity": severity or SEVERITY.get(impact, "moderate"),
            "latest_reported_status": "ongoing", **extra}


# --- Impact provenance --------------------------------------------------------

@pytest.mark.parametrize("impact", ["not_limiting", "limiting", "cant_train"])
def test_impact_is_read_from_the_stored_marker_and_the_parsed_field(impact):
    assert reported_training_impact(_flag(impact)) == impact
    assert reported_training_impact({"training_impact": impact}) == impact


def test_missing_or_legacy_impact_is_unknown_and_stays_restrictive():
    legacy = _flag(None, severity="mild")
    assert reported_training_impact(legacy) == ""
    assert not is_awareness_only(legacy) and restricts_training(legacy)
    assert not is_functional_cant_train(_flag(None, severity="severe"))


# --- not_limiting -------------------------------------------------------------

def test_not_limiting_is_awareness_only():
    assert is_awareness_only(_flag("not_limiting"))
    assert not restricts_training(_flag("not_limiting"))


@pytest.mark.parametrize("override", [
    {"latest_reported_status": "worse"},
    {"medical_hold": True},
    {"restriction_hold": True},
    {"rehab_medical_gate": True},
    {"consequence": "neuro"},
    {"severity": "severe"},
    {"text": "left knee numbness and tingling down the leg"},
    {"injury_type": "cut", "area": "Left shin", "text": "cut on shin"},
])
def test_medical_evidence_overrides_a_not_limiting_choice(override):
    text = override.pop("text", "left knee pain")
    area = override.pop("area", "Left knee")
    flag = _flag("not_limiting", text=text, area=area, **override)
    assert restricts_training(flag)


def _readiness(injuries, title="Hard sparring"):
    session = {"title": title, "session_type": "conditioning", "effective_load": "hard"}
    return build_readiness_adjustment(
        ReadinessCheckin(),
        ReadinessContext(today_session=session,
                         open_injuries=tuple(i for i in injuries if restricts_training(i))),
    )


@pytest.mark.parametrize("title", ["Hard sparring", "Heavy lower body strength", "Easy mobility"])
def test_today_treats_not_limiting_like_no_injury(title):
    clear = _readiness([], title)
    noted = _readiness([_flag("not_limiting", consequence="load_sensitive")], title)
    assert (noted.decision, noted.title, noted.reason) == (clear.decision, clear.title, clear.reason)


def test_today_still_restricts_the_same_injury_when_limiting():
    clear = _readiness([])
    limited = _readiness([_flag("limiting", consequence="structural")])
    assert limited.decision != clear.decision or limited.reason != clear.reason


# --- cant_train ---------------------------------------------------------------

def test_cant_train_stops_normal_training_without_a_severe_medical_label():
    adjustment = _readiness([_flag("cant_train", consequence="load_sensitive")])
    assert adjustment.decision == "pull_back"
    assert "Can't train normally: left knee" in adjustment.reason
    assert "severe" not in adjustment.reason.lower()


def test_genuine_severe_injury_keeps_its_severe_label():
    adjustment = _readiness([_flag(None, severity="severe", consequence="load_sensitive")])
    assert adjustment.decision == "pull_back"
    assert "Active severe injury" in adjustment.reason
    assert injury_stop_phrase(_flag("cant_train", medical_hold=True), "x") == "Active severe injury: x"


def _ankle(impact, **extra):
    return {"id": "a1", "status": "open", "body_area": "Right ankle", "label": "right ankle sprain",
            "description": f"right ankle sprain [training_impact:{impact}]" if impact else "right ankle sprain",
            "severity": "severe", "rehab_type": "sprain", "injury_type": "sprain",
            "latest_reported_status": "ongoing", **extra}


def test_cant_train_keeps_clinically_permitted_rehab_instead_of_a_review_hold():
    policies, bank = load_clinical_policies(), get_rehab_bank()
    functional = resolve_injury_policy(_ankle("cant_train"), policies=policies, bank=bank)
    assert functional["outcome"] != "medical_review"


@pytest.mark.parametrize("injury", [
    _ankle(None),  # legacy severe without provenance stays conservative
    _ankle("cant_train", rehab_medical_gate=True),
    _ankle("cant_train", description="right ankle sprain, suspected fracture [training_impact:cant_train]"),
])
def test_medical_severity_still_holds_for_review(injury):
    decision = resolve_injury_policy(injury, policies=load_clinical_policies(), bank=get_rehab_bank())
    assert decision["outcome"] == "medical_review"


# --- Onboarding / generation --------------------------------------------------

@pytest.mark.parametrize("impact,dropped", [("not_limiting", True), ("limiting", False), ("cant_train", False)])
def test_onboarding_impact_reaches_generation(impact, dropped):
    guided = _build_guided_injury({"area": "Left knee", "severity": {"not_limiting": "low", "limiting": "moderate",
                                                                     "cant_train": "high"}[impact],
                                   "trend": "stable", "injury_type": "pain",
                                   "notes": f"sore [training_impact:{impact}]"})
    injuries, _ = _parse_guided_injury(guided)
    marked = injuries[0].get("training_impact") == "not_limiting"
    assert marked is dropped  # plan_pipeline_runtime removes only these after triage


# --- #2958 metadata: absolute prohibitions vs conditional warnings -------------

def _bank():
    return {row["name"]: row for row in get_exercise_bank() + get_conditioning_bank()}


def _actions(name, region):
    row = _bank()[name]
    return {severity: injury_decision(row, [{"region": region, "severity": severity, "original_phrase": region}],
                                      "SPP", "low").action for severity in ("low", "moderate", "high")}


@pytest.mark.parametrize("name,region", [
    ("Towel/Gi Row Isometric Hold", "elbow"),  # "skip if elbow tendons are already flared..."
    ("Adductor Squeeze Isometric", "groin"),  # "skip if ... increases groin pain beyond..."
])
def test_conditional_contraindication_adapts_with_severity_instead_of_excluding(name, region):
    assert _actions(name, region) == {"low": "allow", "moderate": "modify", "high": "exclude"}


@pytest.mark.parametrize("name,region", [
    ("Jump Rope (Recovery Pace)", "ankle"),  # authored avoid_if region list
    ("Split-Step Ankle Snap Pogo", "foot"),  # avoid_if
    ("Landmine Split-Stance Punch Press", "shoulder"),  # "avoid if shoulder symptoms are active"
    ("Heavy-Bag Single-Shot Clusters", "wrist"),  # "skip if ... symptoms are active"
])
def test_absolute_prohibitions_still_exclude_at_every_severity(name, region):
    assert set(_actions(name, region).values()) == {"exclude"}


# --- Editor: same rules, plus the authored fight cutoff ------------------------

class _Store:
    def __init__(self, flags):
        self.flags = flags

    def list_injury_flags(self, athlete_id, statuses=(), limit=500):
        return [dict(f) for f in self.flags]

    def __getattr__(self, name):  # every other read: nothing recorded
        return lambda *a, **k: []


def _safety(flags, *, fight_date="2026-12-01", day=date(2026, 11, 10)):
    from api.services.exercise_customisation import SafetyContext

    session = {"title": "Conditioning", "blocks": []}
    occurrence = SimpleNamespace(plan_row={"id": "p1", "fight_date": fight_date}, phase="SPP", date=day,
                                 session=session, day={}, week={})
    return SafetyContext(_Store(flags), athlete_id="a1", occurrence=occurrence, equipment=["kettlebell"])


def test_editor_ignores_not_limiting_and_keeps_limiting_restrictions():
    assert not _safety([_flag("not_limiting", area="Left shoulder", text="shoulder")]).has_injuries
    limited = _safety([_flag("limiting", area="Left shoulder", text="shoulder")])
    assert limited.has_injuries and limited.contraindicated(_bank()["Landmine Split-Stance Punch Press"])


@pytest.mark.parametrize("days_out,held", [(22, False), (21, True), (14, True)])
def test_editor_enforces_the_authored_21_day_cutoff(days_out, held):
    row = _bank()["5-10-15 Ladder (Box Jumps/Push-Ups/KB Swings)"]
    day = date(2026, 12, 1).toordinal() - days_out
    assert _safety([], day=date.fromordinal(day)).contraindicated(row) is held


@pytest.mark.parametrize("days_out,held", [(11, False), (10, True)])
def test_editor_enforces_the_authored_10_day_cutoff(days_out, held):
    row = _bank()["Pad EMOM (5x5 format)"]
    assert _safety([], day=date.fromordinal(date(2026, 12, 1).toordinal() - days_out)).contraindicated(row) is held


def test_editor_cutoff_needs_a_fight_and_fails_closed_on_a_bad_date():
    row = _bank()["5-10-15 Ladder (Box Jumps/Push-Ups/KB Swings)"]
    assert not _safety([], fight_date="").contraindicated(row)
    assert _safety([], fight_date="not-a-date").contraindicated(row)


@pytest.mark.parametrize("impact", ["not_limiting", "limiting", "cant_train"])
def test_stored_impact_marker_never_erases_the_injury_identity(impact):
    # "cant"/"not" in the marker used to read as negation: Today flags marked
    # cant_train/not_limiting lost their region and type in the rehab policy.
    from fightcamp.injury_formatting import parse_injury_entry

    parsed = parse_injury_entry(f"Right ankle right ankle sprain [training_impact:{impact}]")
    assert parsed and parsed["canonical_location"] == "ankle" and parsed["injury_type"] == "sprain"
    assert "training_impact" not in parsed["original_phrase"]


def test_cant_train_identity_reaches_rehab_selection():
    decision = resolve_injury_policy({**_ankle("cant_train"), "rehab_type": None, "injury_type": None},
                                     policies=load_clinical_policies(), bank=get_rehab_bank())
    assert decision["region"] == "ankle" and decision["injury_type"] == "sprain"
    assert decision["outcome"] not in {"medical_review", "missing_information"}


# --- Medical checks run before awareness-only (ordering) -----------------------

def test_episode_observations_are_evaluated_before_dropping_a_not_limiting_report(monkeypatch):
    from api.services import today_service

    flag = _flag("not_limiting", area="Left knee")
    monkeypatch.setattr(today_service, "episode_observations", lambda store, athlete_id, row: [{"id": "o1"}])
    monkeypatch.setattr(today_service, "apply_episode_observations",
                        lambda row, obs: {**row, "rehab_medical_gate": True, "progression_assessment_medical_hold": True})
    kept = today_service.restrictive_injuries(object(), "a1", [flag])
    assert kept and kept[0]["rehab_medical_gate"] is True  # the observation's medical hold wins


def test_unreadable_observations_or_context_never_clear_an_injury(monkeypatch):
    from api.services import today_service

    flag = _flag("not_limiting")

    def boom(*args, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(today_service, "episode_observations", boom)
    assert today_service.restrictive_injuries(object(), "a1", [flag]) == [flag]
    assert today_service.restrictive_injuries(object(), "a1", [flag], context_ok=False) == [flag]


def test_clean_observations_let_not_limiting_stay_awareness_only(monkeypatch):
    from api.services import today_service

    monkeypatch.setattr(today_service, "episode_observations", lambda store, athlete_id, row: [])
    assert today_service.restrictive_injuries(object(), "a1", [_flag("not_limiting")]) == []
    limiting = _flag("limiting")
    assert today_service.restrictive_injuries(object(), "a1", [limiting]) == [limiting]


# --- Generation and manual changes share one metadata decision ------------------

def _editor_safety(severity, area, text):
    from api.services.exercise_customisation import SafetyContext

    safety = SafetyContext.__new__(SafetyContext)
    safety.has_injuries, safety.injured_regions = True, {area}
    safety.injury_texts = [f"{area} {text}"]
    safety.guard_injuries = [f"{severity} {area} {text}"]
    safety.phase = "SPP"
    return safety


@pytest.mark.parametrize("name,area", [
    ("Towel/Gi Row Isometric Hold", "elbow"),
    ("Adductor Squeeze Isometric", "groin"),
    ("Punch-Specific Max Isometric Hold", "wrist"),
    ("Overcoming Split-Squat Isometric", "knee"),
    ("Landmine Split-Stance Punch Press", "shoulder"),
    ("Jump Rope (Recovery Pace)", "ankle"),
])
@pytest.mark.parametrize("flag_severity,guard_severity", [("mild", "mild"), ("moderate", "moderate"),
                                                          ("severe", "severe")])
def test_manual_changes_and_generation_reach_the_same_metadata_verdict(name, area, flag_severity, guard_severity):
    row = _bank()[name]
    generation = injury_decision(row, [f"{guard_severity} {area} pain"], "SPP", "moderate").action == "exclude"
    manual = _editor_safety(flag_severity, area, "pain")
    assert (manual.planner_excludes(row) or manual.contraindicated(row)) is generation


@pytest.mark.parametrize("name,area", [("Towel/Gi Row Isometric Hold", "elbow"),
                                       ("Adductor Squeeze Isometric", "groin")])
def test_a_conditional_warning_never_holds_a_manual_change_on_region_alone(name, area):
    safety = _editor_safety("mild", area, "pain")
    assert not safety.contraindicated(_bank()[name])
    assert not safety.planner_excludes(_bank()[name])


# --- End to end: real manual add through the API --------------------------------

def _api_add(monkeypatch, name, flag):
    from api.services.training_library import cache
    from tests.test_exercise_customisation import PLAN_ID, SESSION_ID, _build_client, change, seed_dated

    monkeypatch.setattr("fightcamp.rehab_clinical.load_clinical_policies", lambda: ())
    monkeypatch.setattr("api.services.today_service.load_clinical_policies", lambda: ())
    client, store, _ = _build_client()
    today = seed_dated(store, phase="SPP")
    saved_flag = store.create_injury_flag("athlete-1", flag) if flag else None
    record = next(r for r in cache.get().records if r["entry"]["name"] == name)
    response, _ = change(client, PLAN_ID, action="add", date=today.isoformat(), session_id=SESSION_ID,
                         catalogue_id=record["entry"]["catalogue_id"], dose={"mode": "duration", "duration_seconds": 10})
    if saved_flag:
        # A manual choice must not clear or rewrite its injury evidence.
        assert store.injury_flags["athlete-1"] == [saved_flag]
    return response.status_code


def _api_flag(impact, *, area="shoulder", text="shoulder pain", severity=None):
    marker = f" [training_impact:{impact}]" if impact else ""
    return {"body_area": area, "description": text + marker,
            "severity": severity or SEVERITY.get(impact, "mild")}


@pytest.mark.parametrize("name", ["Landmine Split-Stance Punch Press", "Punch-Specific Max Isometric Hold"])
def test_manual_add_preserves_choice_for_all_three_impact_reports(monkeypatch, name):
    assert _api_add(monkeypatch, name, None) == 200
    assert _api_add(monkeypatch, name, _api_flag("not_limiting")) == 200  # normal training
    # #3037: manual additions/swaps are explicit athlete choices independent
    # of injury guidance. Generation's restrictions are checked above.
    assert _api_add(monkeypatch, name, _api_flag("limiting")) == 200
    assert _api_add(monkeypatch, name, _api_flag("cant_train")) == 200
    assert _api_add(monkeypatch, name, _api_flag(None)) == 200


@pytest.mark.parametrize("flag", [
    _api_flag("not_limiting", text="shoulder pain with numbness and tingling down the arm"),  # red flag
    _api_flag("not_limiting", severity="severe"),  # severe band on a not-limiting report
])
def test_manual_choice_does_not_clear_medical_evidence(monkeypatch, flag):
    from fightcamp.injury_awareness import is_awareness_only

    assert not is_awareness_only(flag)
    # #3037 retains explicit manual choices; this is never medical clearance.
    assert _api_add(monkeypatch, "Landmine Split-Stance Punch Press", flag) == 200


# --- End to end: complete programme generation ----------------------------------

def _plan(monkeypatch, impact):
    from datetime import datetime

    from fightcamp.main import generate_plan_sync
    from tests.planner_clock import pin_planner_clock

    pin_planner_clock(monkeypatch, datetime(2026, 10, 11, 9, 0))
    fields = {"Full name": "Impact Regression", "Age": "26", "Weight (kg)": "70", "Target Weight (kg)": "70",
              "Height (cm)": "178", "Stance": "Orthodox", "Fighting Style (Technical)": "boxing",
              "Professional Status": "amateur", "Current Record": "3-0", "Rounds x Minutes": "3x3",
              "Weekly Training Frequency": "4", "Fatigue Level": "low",
              "Equipment Access": "barbell, dumbbells, heavy_bag, box, bands",
              "Training Availability": "Monday, Tuesday, Thursday, Saturday", "Hard Sparring Days": "Tuesday",
              "What are your key performance goals?": "power, conditioning", "Primary goal": "power",
              "Where do you feel weakest right now?": "gas_tank", "Primary weak area": "gas_tank",
              "When is your next fight?": "2026-11-29", "Athlete Time Zone": "UTC"}
    payload = {"data": {"fields": [{"label": k, "value": v} for k, v in fields.items()]}, "random_seed": 7}
    if impact:
        payload["guided_injuries"] = [{"area": "Left knee", "severity": {"not_limiting": "low", "limiting": "moderate",
                                                                         "cant_train": "high"}[impact],
                                       "trend": "stable", "injury_type": "pain", "timeframe": "last_week",
                                       "notes": f"sore after running [training_impact:{impact}]"}]
    plan = generate_plan_sync(payload)
    if plan["stage2_payload"] is None:
        return plan["status"]
    summary = plan["stage2_payload"]["stage1_selection_summary"]
    return [name for phase in ("GPP", "SPP", "TAPER") for name in summary["strength_names"].get(phase, [])]


def test_generation_follows_the_three_impact_choices(monkeypatch):
    clear = _plan(monkeypatch, None)
    assert _plan(monkeypatch, "not_limiting") == clear  # normal training after triage
    limiting = _plan(monkeypatch, "limiting")
    assert isinstance(limiting, list) and limiting != clear  # knee-specific adaptation, still a full plan
    assert _plan(monkeypatch, "cant_train") == "triage_blocked"  # no normal plan until reviewed
