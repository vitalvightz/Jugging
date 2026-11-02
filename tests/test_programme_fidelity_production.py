"""Replay the recorded production response; no live model or database writes."""
import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
from time import perf_counter
from types import SimpleNamespace

import pytest

from api.models import PlanRequest
from api.stage2_automation import OpenAIStage2Automator
from api.generation.persistence import persist_plan_and_finalize
from api.services.effective_structured_plan import resolve_effective_structured_plan
from api.structured_plan_models import safe_parse_structured_plan
from fightcamp import input_parsing, stage2_planning_brief, strength
from fightcamp.conditioning import _conditioning_structured_profile, get_conditioning_bank
from fightcamp.goal_preservation import collect_goal_evidence
from fightcamp.late_fight_phase_eligibility import _stage1_phase_for_offset
from fightcamp.main import generate_plan_sync
from fightcamp.physical_session_frequency import cadence_windows, dated_appointments, appointment_budget_exceeded
from fightcamp.programme_fidelity import programme_fidelity_findings, reconcile_authoritative_programme
from fightcamp.stage2_validator import _goal_witness_dose_matches
from fightcamp.strength import _classify_prescription_type, _prescription_templates
from fightcamp.working_dose import working_dose
from support import FakeOpenAIClient, FakeStore

FIXTURES = Path(__file__).parent / "fixtures" / "programme_fidelity"

# The recorded render was written for the strength bank production used on
# 2026-10-14. Replays regenerate against that bank: rows added since are
# removed and later phase edits undone, so the recording still matches.
ROWS_ADDED_AFTER_RECORDING = frozenset({
    "Tempo Push-Up (3-1-1)", "Feet-Elevated Push-Up", "Pike Push-Up", "Band Bent-Over Row",
    "Half-Kneeling Single-Arm Band Row", "Split Squat (Bodyweight Tempo 3-1-1)",
})
PHASES_AT_RECORDING = {"Bulgarian Split Squat": ["SPP"]}


def _bank_at_recording():
    rows = []
    for row in deepcopy(strength.get_exercise_bank()):
        if row["name"] in ROWS_ADDED_AFTER_RECORDING:
            continue
        if row["name"] in PHASES_AT_RECORDING:
            row["phases"] = list(PHASES_AT_RECORDING[row["name"]])
        rows.append(row)
    return rows


@pytest.fixture(scope="module")
def production_replay():
    intake = json.loads((FIXTURES / "affected_intake.json").read_text(encoding="utf8"))
    # Production requests carried no seed at the time, so the source is
    # regenerated unseeded (name-order ties), as production ran it; the
    # recorded model response and authority are separate.
    intake["random_seed"] = None
    with pytest.MonkeyPatch.context() as patch:
        recorded_bank = _bank_at_recording()
        patch.setattr(strength, "get_exercise_bank", lambda: recorded_bank)
        patch.setattr(input_parsing, "_utc_now", lambda: datetime(2026, 10, 14, 3, 40))
        patch.setattr(stage2_planning_brief, "_utc_now", lambda: datetime(2026, 10, 14, 3, 40))
        stage1 = generate_plan_sync(PlanRequest(**intake).to_payload())
        patch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")
        raw = (FIXTURES / "production_render.txt").read_text(encoding="utf8")
        client = FakeOpenAIClient([SimpleNamespace(id="recorded_response", output_text=raw,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2))])
        final = asyncio.run(OpenAIStage2Automator(client=client, model="recorded").finalize(stage1_result=stage1))
    return intake, stage1, final, client


def test_recorded_authority_proves_missing_sessions_and_wrong_dates():
    brief = json.loads((FIXTURES / "recorded_authority.json").read_text(encoding="utf8"))
    raw = (FIXTURES / "production_render.txt").read_text(encoding="utf8")
    before = programme_fidelity_findings(brief, raw)
    assert any(f.get("scheduled_d_day") == 42 for f in before)
    assert "D-40 (Wednesday)" in raw and "D-26 (Thursday)" in raw
    fixed = reconcile_authoritative_programme(brief, raw)
    assert fixed["findings"] == []
    assert "D-40 (Monday)" in fixed["text"] and "D-26 (Monday)" in fixed["text"]
    for day in (42, 35, 28, 21):
        assert re.search(rf"D-{day} \(Saturday\).*Hard Sparring", fixed["text"], re.I)
    assert "### D-38 (Wednesday)" in fixed["text"]  # Original S&C day retained.


def test_intake_through_finalizer_preserves_real_goals_and_all_appointments(production_replay):
    _, stage1, final, client = production_replay
    brief = stage1["planning_brief"]
    assert len(client.responses.calls) == 1
    # This historical model response predates required AI-authored Easier.
    # Reconciliation must not fabricate it; all programme/safety checks remain clean.
    assert all(error["code"] == "exercise_coaching_missing" for error in final["stage2_validator_report"]["errors"])
    assert programme_fidelity_findings(brief, final["final_plan_text"]) == []
    assert final["stage2_validator_report"]["programme_reconciliation"]["model_call_used"] is False
    goals = {g["goal"]: g for g in brief["goal_preservation"]}
    assert goals["speed"]["state"] == "build" and goals["speed"]["satisfied"]
    assert goals["power"]["satisfied"] and goals["conditioning"]["satisfied"]
    evidence = collect_goal_evidence(brief)
    bag = next(e for e in evidence if e.get("name") == "Max-Power Bag Burst")
    assert bag["work_sec"] == 5 and bag["rounds"] == 7 and bag["rest_sec"] == 60
    assert {"speed_quality", "ballistic_power"}.issubset(bag["intents"])
    assert any(e.get("name") == "Easy Assault Bike" for e in evidence)
    assert "Depth Drop (No Rebound)" not in final["final_plan_text"]
    assert "Weight information is incomplete" in final["final_plan_text"]
    assert "increase fight-specific repeatability and power transfer" in final["final_plan_text"]
    assert "Nasal breathing only; excellent recovery modality" in final["final_plan_text"]
    assert "Stop the burst if punch speed, shape or head position drops" in final["final_plan_text"]
    for day in (42, 35, 28, 21):
        assert re.search(rf"D-{day} \(Saturday\).*Hard Sparring", final["final_plan_text"], re.I)


def test_count_discrepancies_remain_visible_across_progression_boundaries(production_replay):
    _, stage1, _, _ = production_replay
    role_map = stage1["planning_brief"]["weekly_role_map"]
    appointments = dated_appointments(role_map)
    windows = cadence_windows([(d["d_day"], d["weekday"]) for w in role_map["weeks"] for d in w["calendar_days"]])
    counts = [len({r["countdown_offset"] for r in appointments} & {d for d, _ in days}) for days in windows]
    assert appointment_budget_exceeded(role_map, 6) == (max(counts) > 6)
    warnings = production_replay[2]["stage2_validator_report"]["warnings"]
    budget_warnings = [w for w in warnings if w["code"] == "total_session_budget_exceeded"]
    assert bool(budget_warnings) == (max(counts) > 6)
    for warning in budget_warnings:
        offsets = set(range(warning["min_d_day"], warning["max_d_day"] + 1))
        scheduled = [r for r in appointments if r["countdown_offset"] in offsets]
        assert warning["scheduled_training_days"] == len({r["countdown_offset"] for r in scheduled})
        assert warning["scheduled_sessions"] == len(scheduled)
    # A legitimate same-day coach + S&C pair consumes two appointments but one day.
    assert sum(r["countdown_offset"] == 44 for r in appointments) == 2
    extra = deepcopy(role_map)
    first = extra["weeks"][0]
    first["session_roles"].append({"category": "strength", "role_key": "extra_strength", "countdown_offset": 50})
    extra_counts = [len({r["countdown_offset"] for r in dated_appointments(extra)} & {d for d, _ in days})
                    for days in windows]
    assert appointment_budget_exceeded(extra, 6) == (max(extra_counts) > 6)


@pytest.mark.parametrize("mutation", ["weekday", "missing", "exercise", "rest", "phase", "invented", "order"])
def test_post_finalization_drift_is_still_rejected(production_replay, mutation):
    _, stage1, final, _ = production_replay
    text = final["final_plan_text"]
    changes = {
        "weekday": lambda t: t.replace("D-42 (Saturday)", "D-42 (Wednesday)"),
        "missing": lambda t: re.sub(r"### D-42 .*?(?=\n### |\n## Week )", "", t, flags=re.S),
        "exercise": lambda t: t.replace("Max-Power Bag Burst", "Assault Bike Sprint"),
        "rest": lambda t: t.replace("5s work, 60s rest x 7 rounds", "5s work, 20s rest x 7 rounds"),
        "phase": lambda t: t.replace("## Week 1 — GPP", "## Week 1 — SPP"),
        "invented": lambda t: t + "\n### D-999 (Monday) — Extra session\n- Squat — 3 x 10\n",
        "order": lambda t: re.sub(r"(### D-51 .*?)(### D-50 .*?)(?=\n### )", r"\2\1", t, count=1, flags=re.S),
    }
    changed = changes[mutation](text)
    assert changed != text
    assert programme_fidelity_findings(stage1["planning_brief"], changed)


def _persist_final(intake, final):
    request = PlanRequest(**intake)
    store = FakeStore()
    store.profiles["athlete-1"] = {"id": "athlete-1", "full_name": "Programme Fidelity Regression"}
    job = store.create_or_get_generation_job(athlete_id="athlete-1", client_request_id="fidelity-replay",
        source="web_intake", request_payload=request.model_dump(mode="json"))
    job = store.update_generation_job(job["id"], status="running")
    async def direct(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    asyncio.run(persist_plan_and_finalize(job=job, job_id=job["id"], athlete_id="athlete-1",
        plan_id=None, intake_id=None, job_source="web_intake", resume_from_job_only=False,
        admin_resume_plan_row=None, final_result=deepcopy(final), request_body=request, store=store,
        emit_milestone=lambda *_args, **_kwargs: None, to_thread_with_heartbeat=direct, t_start=perf_counter()))
    assert len(store.plans) == 1
    saved = next(iter(store.plans.values()))
    return saved


def test_persistence_saves_corrected_programme_without_changing_visibility_policy(production_replay):
    intake, stage1, final, _ = production_replay
    saved = _persist_final(intake, final)
    assert saved["plan_text"] == final["final_plan_text"]
    assert programme_fidelity_findings(stage1["planning_brief"], saved["plan_text"]) == []


def test_inclusive_phase_allocation_matches_calendar_length():
    model = {"days_until_fight": 52, "phase_weeks": {"days": {"GPP": 15, "SPP": 22, "TAPER": 15}}}
    phases = [_stage1_phase_for_offset(model, d) for d in range(52)]
    assert {phase: phases.count(phase) for phase in ("GPP", "SPP", "TAPER")} == model["phase_weeks"]["days"]
    assert phases[14] == "TAPER" and phases[15] == "SPP"
    assert phases[36] == "SPP" and phases[37] == "GPP"


def test_unpriced_closed_session_is_a_fidelity_failure(production_replay):
    _, stage1, final, _ = production_replay
    brief = deepcopy(stage1["planning_brief"])
    role = next(r for w in brief["weekly_role_map"]["weeks"] for r in w["session_roles"]
                if r.get("category") == "strength" and r.get("selected_exercise_assignments"))
    role["effective_strength_prescriptions"] = []
    findings = programme_fidelity_findings(brief, final["final_plan_text"])
    assert any(f["code"] == "authoritative_programme_unpriced_session" for f in findings)


def test_interactive_fallback_preserves_calendar_doses_and_coaching(production_replay):
    _, stage1, final, _ = production_replay
    brief = stage1["planning_brief"]
    card = resolve_effective_structured_plan({"planning_brief": brief, "plan_text": final["final_plan_text"]})
    assert card and safe_parse_structured_plan(card).ok
    assert card["plan_metadata"]["sport"] == "boxing"
    assert card["plan_metadata"]["timezone"] == "Europe/London"
    assert "heavy_bag" in card["athlete_context"]["equipment_access"]
    assert all(week["week_goal"] == brief["phase_strategy"][week["phase_label"]]["visible_objective"]
               for week in card["weeks"])
    days = {int(day["countdown_label"][2:]): day for week in card["weeks"] for day in week["days"]}
    for week in brief["weekly_role_map"]["weeks"]:
        for date in week["calendar_days"]:
            day = days[date["d_day"]]
            assert day["weekday"].lower() == date["weekday"][:3].lower()
            assert day["planning_week_phase"] == week["phase"]
            from datetime import timedelta
            assert day["date"] == (datetime.fromisoformat(brief["athlete_snapshot"]["fight_date"]).date()
                                   - timedelta(days=date["d_day"])).isoformat()
    for offset in (42, 35, 28, 21):
        today = days[offset]["today_card"]
        assert "hard sparring" in f"{today.get('headline', '')} {today.get('coach_led_contact', '')}".lower()
    blocks = {(offset, b["display_name"]): b for offset, day in days.items()
              for session in day["sessions"] for b in session["blocks"]}
    from fightcamp.calendar_context import role_d_day
    for week in brief["weekly_role_map"]["weeks"]:
        for role in week["session_roles"]:
            offset = role_d_day(week, role)
            if offset is not None:
                for member in role.get("selected_exercise_assignments") or []:
                    assert (offset, member["name"]) in blocks
    carry = blocks[44, "Sled Pull Forward (Harness)"]
    assert carry["sets"] == 3 and carry["distance"] == {"value": 20, "unit": "meters"}
    hold = blocks[44, "Staggered Stance Hold"]
    assert hold["sets"] == 2 and hold["duration"] == {"value": 20, "unit": "seconds"}
    assert hold["effort"]["value"] == 6 and hold["rest"]["value"] == 60
    bag = next(block for (_, name), block in blocks.items() if name == "Max-Power Bag Burst")
    assert bag["rounds"] == 7 and bag["work"] == {"value": 5, "unit": "seconds"}
    assert bag["rest"] == {"value": 60, "unit": "seconds"} and bag["effort"]["value"] == 8
    assert any("Stop the burst if punch speed" in rule for rule in bag["stop_rules"])
    thrusters = [block for (day, name), block in blocks.items() if name == "Barbell Thruster" and day > 17]
    sleds = [block for (day, name), block in blocks.items() if name == "Sled Push (Speed)" and day > 17]
    assert thrusters and sleds
    for thruster in thrusters:
        assert thruster["sets"] == 4 and thruster["reps"] == 2 and thruster["rest"]["value"] == 120
    for sled in sleds:
        assert sled["sets"] == 6 and sled["duration"] == {"value": 5, "unit": "seconds"}
        assert sled["rest"]["value"] == 90 and "light sled" in sled["execution_instructions"]
    # A saved card must expose the same interactive values as reconstruction.
    stored = resolve_effective_structured_plan({"structured_plan": card, "planning_brief": brief,
                                                "plan_text": final["final_plan_text"]})
    assert stored == card


@pytest.mark.parametrize("tamper", [False, "rest", "missing_exercise"])
def test_provider_card_conversion_uses_the_reconciled_programme(production_replay, monkeypatch, tamper):
    intake, stage1, final, _ = production_replay
    card = resolve_effective_structured_plan({"planning_brief": stage1["planning_brief"],
                                              "plan_text": final["final_plan_text"]})
    if tamper == "rest":
        bag = next(b for w in card["weeks"] for d in w["days"] for s in d["sessions"] for b in s["blocks"]
                   if b["display_name"] == "Max-Power Bag Burst")
        bag["rest"]["value"] = 20
    elif tamper == "missing_exercise":
        session = next(s for w in card["weeks"] for d in w["days"] for s in d["sessions"]
                       if any(b["display_name"] == "Sled Push (Speed)" for b in s["blocks"]))
        session["blocks"] = [b for b in session["blocks"] if b["display_name"] != "Sled Push (Speed)"]
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    client = FakeOpenAIClient([SimpleNamespace(id="recorded_text", output_text=(FIXTURES / "production_render.txt").read_text(encoding="utf8"),
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2)),
        SimpleNamespace(id="card_conversion", output_text=json.dumps(card),
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2)),
        SimpleNamespace(id="card_retry", output_text=json.dumps(card),
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2))])
    converted = asyncio.run(OpenAIStage2Automator(client=client, model="recorded").finalize(stage1_result=stage1))
    if not tamper:
        assert converted.get("structured_plan") is not None, converted["stage2_validator_report"].get("structured_plan")
        assert safe_parse_structured_plan(converted["structured_plan"]).ok
        assert len(client.responses.calls) == 2
    saved = _persist_final(intake, converted)
    effective = resolve_effective_structured_plan(saved)
    assert effective and safe_parse_structured_plan(effective).ok
    bag = next(b for w in effective["weeks"] for d in w["days"] for s in d["sessions"] for b in s["blocks"]
               if b["display_name"] == "Max-Power Bag Burst")
    assert bag["rest"] == {"value": 60, "unit": "seconds"}
    assert sum(b["display_name"] == "Sled Push (Speed)" for w in effective["weeks"] for d in w["days"]
               for s in d["sessions"] for b in s["blocks"]) == 3
    assert programme_fidelity_findings(stage1["planning_brief"], converted["final_plan_text"]) == []


def test_late_goal_repair_retains_earlier_speed_appointments(monkeypatch):
    from test_combat_training_allocation import run_lead
    _, brief = run_lead(monkeypatch, status="professional", training_frequency=9, fatigue="low",
                        hard_sparring_days=[], support_work_days=["Tuesday", "Thursday", "Friday"])
    assert all(g["satisfied"] for g in brief["goal_preservation"])
    evidence = collect_goal_evidence(brief)
    assert any((e.get("d_day") or 0) > 21 and "speed_quality" in e["intents"]
               and e["development_quality"] for e in evidence)
    assert any(2 <= (e.get("d_day") or 0) <= 15 and "ballistic_power" in e["intents"] for e in evidence)


def test_exercise_method_controls_dose_and_braking_does_not_develop_speed():
    assert _classify_prescription_type({"name": "Landmine Push Press", "equipment": ["landmine"], "tags": ["core"]}) == "ballistic"
    hold_type = _classify_prescription_type({"name": "Staggered Stance Hold", "tags": ["core"]})
    hold = _prescription_templates("SPP")[hold_type]
    assert "hold" in hold and "tempo" not in hold and "reps" not in hold
    sled = _prescription_templates("SPP")[_classify_prescription_type({"name": "Sled Push (Speed)"})]
    assert "light sled" in sled and "5s" in sled and "rest 90s" in sled
    drop = next(d for d in get_conditioning_bank() if d["name"] == "Depth Drop (No Rebound)")
    # This is a rep-based landing dose, not timed alactic work.
    assert not _conditioning_structured_profile(drop)["alactic_structure"]
    assert not _conditioning_structured_profile(drop)["speed_development"]


def test_working_dose_does_not_inflate_authorised_volume_or_corrupt_tempo():
    assert working_dose("3–5x4–6 reps; rest 60–90s; RPE 6–8") == "3x4 reps; rest 90s; RPE 6"
    assert working_dose("2-4 sets x 6-10 reps, tempo 3-1-3") == "2 sets x 6 reps, tempo 3-1-3"
    assert working_dose("tempo 3–1–3") == "tempo 3–1–3"
    witness = {"work_sec": 5, "rest_sec": 60, "rounds": 8}
    assert _goal_witness_dose_matches(witness, "5s work, 60s rest x 8 rounds; RPE 8")
    assert not _goal_witness_dose_matches(witness, "5s work, 20s rest x 8 rounds; RPE 8")
    assert not _goal_witness_dose_matches(witness, "3s work, 60s rest x 8 rounds; RPE 8")


def test_a_label_or_low_effort_bag_touch_cannot_clear_power(production_replay):
    _, stage1, _, _ = production_replay
    brief = deepcopy(stage1["planning_brief"])
    for week in brief["weekly_role_map"]["weeks"]:
        for role in week["session_roles"]:
            for member in role.get("selected_exercise_assignments", []):
                if member["name"] == "Max-Power Bag Burst":
                    member["rpe"] = 3
    bag = next(e for e in collect_goal_evidence(brief) if e.get("name") == "Max-Power Bag Burst")
    assert "ballistic_power" not in bag["intents"]
    assert not bag["development_capable"]


@pytest.mark.parametrize("name,created_at", [
    ("earlier_before_notes", datetime(2026, 10, 13, 19, 26)),
    ("earlier_with_notes", datetime(2026, 10, 13, 19, 49)),
])
def test_earlier_saved_intakes_and_recorded_responses_have_the_same_fidelity_contract(name, created_at):
    intake = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf8"))
    intake["random_seed"] = None  # unseeded, as production ran it
    with pytest.MonkeyPatch.context() as patch:
        recorded_bank = _bank_at_recording()
        patch.setattr(strength, "get_exercise_bank", lambda: recorded_bank)
        patch.setattr(input_parsing, "_utc_now", lambda: created_at)
        patch.setattr(stage2_planning_brief, "_utc_now", lambda: created_at)
        patch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")
        stage1 = generate_plan_sync(PlanRequest(**intake).to_payload())
        raw = (FIXTURES / f"{name}_render.txt").read_text(encoding="utf8")
        client = FakeOpenAIClient([SimpleNamespace(id="recorded_response", output_text=raw,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2))])
        final = asyncio.run(OpenAIStage2Automator(client=client, model="recorded").finalize(stage1_result=stage1))
    brief = stage1["planning_brief"]
    assert brief["athlete_snapshot"]["training_frequency"] == 6
    day_overrun = appointment_budget_exceeded(brief["weekly_role_map"], 6)
    assert any(w["code"] == "total_session_budget_exceeded" for w in final["stage2_validator_report"]["warnings"]) == day_overrun
    assert programme_fidelity_findings(brief, final["final_plan_text"]) == []
    # This historical model response predates required AI-authored Easier.
    # Reconciliation must not fabricate it; all programme/safety checks remain clean.
    assert all(error["code"] == "exercise_coaching_missing" for error in final["stage2_validator_report"]["errors"])
    assert all(g["satisfied"] for g in brief["goal_preservation"])
    assert "Max-Power Bag Burst" in final["final_plan_text"]
    if name == "earlier_with_notes":
        # The real pipeline must preserve both sustained combination rounds
        # and short full-rest bursts, rather than one repeated modality.
        bag = [e for e in collect_goal_evidence(brief) if "bag" in e.get("name", "").lower()]
        assert {e["name"] for e in bag} == {"Heavy Bag Power Rounds", "Max-Power Bag Burst"}
        assert any(e.get("work_sec") == 180 and 3 <= e["rounds"] <= 5 for e in bag)
        assert any(e["work_sec"] == 5 and e["rest_sec"] == 60 for e in bag)


def test_recorded_finalizer_and_served_fallback_do_not_restore_retired_mental_tasks(production_replay):
    from collections import Counter
    from fightcamp.calendar_context import role_d_day
    from fightcamp.mental_training_policy import mental_category

    _, stage1, final, client = production_replay
    brief = stage1['planning_brief']
    card = resolve_effective_structured_plan({'planning_brief': brief, 'plan_text': final['final_plan_text']})
    expected = Counter(
        (role_d_day(week, role), mental_category(role))
        for week in brief['weekly_role_map']['weeks'] for role in week.get('session_roles', [])
        if mental_category(role)
    )
    actual = Counter()
    for week in card['weeks']:
        for day in week['days']:
            offset = int(day['countdown_label'][2:])
            for session in day.get('sessions') or []:
                title = str(session.get('title') or '').lower()
                category = ('visualise' if 'visualis' in title or 'visualiz' in title else
                            'analyse' if 'tactical' in title else 'reflect' if 'self-review' in title else None)
                if category:
                    actual[offset, category] += 1
    assert actual == expected
    assert max(actual.values()) == 1
    assert len(actual) < 16  # The previous partial audit omitted the final governor.
