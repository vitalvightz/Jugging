"""Cadence and daily reservations use frozen work, never ordinary training."""
from copy import deepcopy

import pytest

from api.contracts.injury_policy import resolve_injury_policy, reconcile_session_prescription
from api.contracts.rehab_schedule import schedule_rehab
from api.contracts.rehab_completion import completed_dose_from_session
from fightcamp.rehab_clinical import load_clinical_policies
from fightcamp.rehab_protocols import get_rehab_bank


def setup(region="ankle", stage="restore"):
    row = dict(id="i", episode_id="e", athlete_id="a", canonical_location=region, body_region=region,
               body_area="Left " + region, side="left", injury_type="sprain" if region == "ankle" else "strain",
               severity="mild", status="monitoring", latest_reported_status="improving", rehab_stage=stage,
               updated_at="2026-09-20T00:00:00Z")
    decision = resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(), phase="TAPER")
    assert decision["prescription"] and decision["activation"] == "live"
    return row, decision


def accepted(decision, day="2026-09-30", status="done", plan="old"):
    snapshot = reconcile_session_prescription(None, decisions=[decision], plan_id=plan, training_day=day)
    return dict(id="c", athlete_id="a", plan_id=plan, training_day=day, status=status, prescription_snapshot=snapshot)


@pytest.mark.parametrize("status", ["started", "done", "modified"])
def test_any_started_performed_or_stopped_work_reserves_episode_across_plans(status):
    row, decision = setup()
    completion = accepted(decision, status=status)
    result = schedule_rehab(row, decision, training_day="2026-09-30", completions=[completion])
    assert result["state"] == "already_completed"
    row["episode_id"] = "reopened"
    assert schedule_rehab(row, decision, training_day="2026-09-30", completions=[completion])["state"] == "due"


def test_alternate_days_and_missed_days_do_not_accumulate_volume():
    row, decision = setup()
    assert decision["prescription"]["minimum_gap_days"] == 2
    completion = accepted(decision)
    assert schedule_rehab(row, decision, training_day="2026-10-01", completions=[completion]) == {
        "state": "recovery_day", "reason": "Allow the configured recovery gap before repeating this routine.", "next_due_day": "2026-10-02"}
    for day in ["2026-10-02", "2026-10-08"]:
        assert schedule_rehab(row, decision, training_day=day, completions=[completion])["state"] == "due"
    assert accepted(decision, day="2026-10-08")["prescription_snapshot"]["session"]["blocks"][0]["dose"] == {}


def test_skipped_work_has_no_credit_and_daily_chest_remains_in_taper():
    row, decision = setup("chest", "calm")
    assert decision["stage"] == "calm" and decision["prescription"]["minimum_gap_days"] == 1
    completion = accepted(decision, status="skipped")
    assert schedule_rehab(row, decision, training_day="2026-09-30", completions=[completion])["state"] == "due"
    completion["status"] = "done"
    assert schedule_rehab(row, decision, training_day="2026-10-01", completions=[completion])["state"] == "due"
    dose = completed_dose_from_session({**completion, "rehab_performance": "done_as_shown"}, prescribed={})
    assert dose.completion_state == "performed_amount_unknown" and dose.reps is None and dose.sets is None


def test_known_hard_training_defers_loading_and_is_not_rehab_credit():
    row, decision = setup()
    training = {"blocks": [{"block_type": "strength", "mechanical_load_regions": ["ankle"], "load": "high"}]}
    assert schedule_rehab(row, decision, training_day="2026-10-01", training_session=training)["state"] == "deferred"
    normal = dict(athlete_id="a", training_day="2026-10-01", status="done", session_id="strength")
    assert schedule_rehab(row, decision, training_day="2026-10-01", completions=[normal])["state"] == "due"
    assert schedule_rehab(row, decision, training_day="2026-10-01", readiness_decision="pull_back")["state"] == "due"


@pytest.mark.parametrize("stage", ["calm", "restore", "load"])
@pytest.mark.parametrize("readiness", ["stop", "pull_back", "not_checked_in"])
def test_training_readiness_never_holds_phase_rehab(stage, readiness):
    row, decision = setup()
    decision["stage"] = stage
    assert schedule_rehab(row, decision, training_day="2026-10-01", readiness_decision=readiness)["state"] == "due"


def test_unknown_response_holds_loading_until_explicit_injury_improvement():
    row, decision = setup()
    event = dict(athlete_id="a", created_at="2026-09-30T12:00:00Z", event_json=dict(
        injury_id="i", injury_episode_id="e", occurred_at="2026-09-30T00:00:00Z", response={}))
    assert schedule_rehab(row, decision, training_day="2026-10-02", exposures=[event])["state"] == "held"
    row.update(latest_reported_status="improving", updated_at="2026-10-01T12:00:00Z")
    assert schedule_rehab(row, decision, training_day="2026-10-02", exposures=[event])["state"] == "due"


def test_accepted_prescription_keeps_its_previous_gap_after_policy_change():
    row, decision = setup()
    previous = accepted(decision)
    changed = deepcopy(decision)
    changed["prescription"]["minimum_gap_days"] = 1
    assert schedule_rehab(row, changed, training_day="2026-10-01", completions=[previous])["state"] == "recovery_day"


def test_resuming_a_frozen_alternative_does_not_reselect_the_primary_routine():
    row, primary = setup()
    balance = resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(),
        excluded_drill_ids=[primary["prescription"]["drill_id"]])
    assert balance["prescription"]["drill_id"] == "ankle_sprain_supported_balance"
    frozen = reconcile_session_prescription(None, decisions=[balance], plan_id="plan", training_day="2026-09-30")
    primary["schedule"] = {"state": "already_completed"}
    resumed = reconcile_session_prescription(None, decisions=[primary], plan_id="plan", training_day="2026-09-30", frozen=frozen)
    assert not resumed["safety_hold"] and resumed["session"] == frozen["session"]
    primary["loading_hold"] = True
    assert reconcile_session_prescription(None, decisions=[primary], plan_id="plan", training_day="2026-09-30", frozen=frozen)["safety_hold"]


def test_structured_load_does_not_crash_rehab_scheduling():
    row, decision = setup()
    training = {"effective_load": "high", "blocks": [{"block_type": "strength",
        "mechanical_load_regions": ["ankle"], "load": {"method": "percentage", "value": 80}}]}
    assert schedule_rehab(row, decision, training_day="2026-10-01", training_session=training)["state"] == "deferred"
