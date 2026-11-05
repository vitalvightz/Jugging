"""Opt-in stage bundles use the existing scheduler and exposure contracts."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from api.contracts.injury_policy import rehab_allocation_count, reconcile_session_prescription, resolve_injury_policy
from api.contracts.rehab_completion import (
    build_rehab_exposure_event, build_rehab_response_prompts, resolve_rehab_completion,
)
from api.contracts.rehab_schedule import schedule_rehab
from api.services.rehab_completion_service import record_rehab_exposures, session_rehab_items
from tests.support import FakeStore
from fightcamp.rehab_clinical import ClinicalPolicy, content_hash, load_clinical_policies, policy_review_hash, validate_clinical_bank
from fightcamp.rehab_protocols import get_rehab_bank

DAY = "2026-10-02"
PLAN = str(uuid4())
ATHLETE = str(uuid4())
IDS = ["ankle_sprain_supported_balance", "ankle_sprain_heel_lowering"]


def fixture(bundle=True):
    policy = next(p for p in load_clinical_policies() if p.policy_id == "ankle_sprain")
    if not bundle:
        # Keep the pre-bundle v3 policy explicit; default tests use live content.
        from tests.rehab_pathway_migration import composed
        policy = next(p for p in composed() if p.policy_id == "ankle_sprain")
        raw = policy.model_dump()
        raw.update(version=3, status="draft", activation="shadow", content_hash=None,
                   stage_bundles={})
        draft = ClinicalPolicy.model_validate(raw)
        policy = ClinicalPolicy.model_validate({
            **draft.model_dump(), "status": "active", "activation": "live",
            "content_hash": policy_review_hash(draft),
        })
    injury = dict(id=str(uuid4()), episode_id=str(uuid4()), athlete_id=ATHLETE,
                  canonical_location="ankle", body_region="ankle", body_area="Left ankle",
                  side="left", injury_type="sprain", severity="mild", status="monitoring",
                  latest_reported_status="improving", rehab_stage="restore",
                  created_at="2026-09-30T00:00:00Z", updated_at="2026-09-30T00:00:00Z")
    bank = get_rehab_bank()
    if not bundle:
        from tools.rehab_metadata_review_lib import before_bank_publication
        bank = before_bank_publication(bank, "drills")
    decision = resolve_injury_policy(injury, policies=(policy,), bank=bank)
    return policy, injury, decision


def snapshot(decisions, session=None, frozen=None):
    return reconcile_session_prescription(session, decisions=decisions, plan_id=PLAN,
                                         training_day=DAY, frozen=frozen)


def test_single_drill_shape_identity_dose_and_hash_are_unchanged():
    policy, _, decision = fixture(bundle=False)
    assert policy_review_hash(policy) == policy.content_hash
    assert policy.content_hash == "ae9a7431741198a66fa56426e21e467d47b0a3639aebdf13846b633590a193dd"
    current = decision["prescription"]
    assert current["drill_id"] == IDS[1] and current["dose"] == {}
    assert "drills" not in current
    blocks = snapshot([decision])["session"]["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["block_id"] == f"rehab:{decision['injury_id']}:{decision['injury_episode_id']}"
    assert "rehab_allocation_id" not in blocks[0]


def test_bundle_expands_all_reviewed_drills_once_and_preserves_order():
    _, _, decision = fixture()
    assert [d["drill_id"] for d in decision["prescription"]["drills"]] == IDS
    first = snapshot([decision])
    blocks = first["session"]["blocks"]
    assert [b["rehab_drill_id"] for b in blocks] == IDS
    assert len({b["block_id"] for b in blocks}) == 2
    assert len({b["rehab_allocation_id"] for b in blocks}) == 1
    assert rehab_allocation_count(blocks) == 1
    assert all(b["minimum_gap_days"] == 2 for b in blocks)
    repeated = snapshot([decision], first["session"])
    assert repeated["session"]["blocks"] == blocks
    assert not snapshot([decision], frozen=first)["safety_hold"]


@pytest.mark.parametrize("session_type,allocated", [("rehab", 2), ("sparring", 1)])
def test_bundle_uses_one_allocation_and_keeps_sparring_ceiling(session_type, allocated):
    _, _, decision = fixture()
    second = deepcopy(decision)
    second.update(injury_id=str(uuid4()), injury_episode_id=str(uuid4()))
    first = snapshot([decision], dict(session_id="s", session_type=session_type, blocks=[
        dict(block_id="training", block_type="strength", mechanical_load_regions=["shoulder"], contact_level="none"),
    ]))
    result = snapshot([decision, second], first["session"])
    assert result["allocation_limit"] == allocated
    assert sum(b["block_type"] == "rehab" for b in result["session"]["blocks"]) == allocated * len(IDS)
    assert sum(b.get("injury_id") == decision["injury_id"] for b in result["session"]["blocks"]) == 2
    assert any(c.get("injury_id") == second["injury_id"] and c["action"] == "deferred"
               for c in result["changes"]) == (allocated == 1)


@pytest.mark.parametrize("state", ["held", "deferred", "recovery_day"])
def test_schedule_gate_applies_to_entire_bundle(state):
    _, _, decision = fixture()
    decision["schedule"] = {"state": state}
    assert snapshot([decision]) is None


def test_loading_readiness_training_and_medical_gates_hold_whole_bundle():
    _, injury, decision = fixture()
    assert decision["prescription"]["is_loading"]
    assert schedule_rehab(injury, decision, training_day=DAY, readiness_decision="pull_back")["state"] == "due"
    assert schedule_rehab(injury, decision, training_day=DAY, training_session={
        "blocks": [dict(block_type="strength", mechanical_load_regions=["ankle"], load="high")]
    })["state"] == "deferred"
    first = snapshot([decision])
    changed = deepcopy(decision)
    changed["readiness_hold"] = True  # the daily verdict never freezes started rehab
    assert not snapshot([changed], frozen=first)["safety_hold"]
    changed = deepcopy(decision)
    changed["outcome"] = "medical_review"
    assert snapshot([changed], frozen=first)["safety_hold"]
    # Check every accepted member: a hold on the first cannot be hidden by the last.
    first["session"]["blocks"][0]["injury_episode_id"] = str(uuid4())
    assert snapshot([decision], frozen=first)["safety_hold"]


def test_cadence_recognizes_any_member_and_reserves_the_episode_once():
    policy, injury, decision = fixture()
    previous = resolve_injury_policy(injury, policies=(policy,), bank=get_rehab_bank(),
                                     excluded_drill_ids=[IDS[1]])
    assert previous["prescription"] is None  # atomic combination, no silent substitute
    _, _, single = fixture(bundle=False)
    single["injury_id"], single["injury_episode_id"] = injury["id"], injury["episode_id"]
    completion = dict(athlete_id=ATHLETE, training_day=DAY, status="started",
                      prescription_snapshot=snapshot([single]))
    assert schedule_rehab(injury, decision, training_day=DAY, completions=[completion])["state"] == "already_completed"
    completion.update(status="done", training_day="2026-10-01")
    # Even when the primary bundle member is balance, yesterday's heel lowering wins.
    assert schedule_rehab(injury, decision, training_day=DAY, completions=[completion])["state"] == "recovery_day"


@pytest.mark.parametrize("performance,state", [
    ("done_as_shown", "performed_amount_unknown"),
    ("changed", "partial_amount_unknown"),
    ("stopped", "partial_amount_unknown"),
])
def test_completion_produces_valid_distinct_exposures_and_one_response_group(performance, state):
    _, injury, decision = fixture()
    accepted = snapshot([decision])
    completion = dict(status="done" if performance == "done_as_shown" else "modified",
                      rehab_performance=performance, prescription_snapshot=accepted)
    items = session_rehab_items({"id": PLAN}, training_day=DAY,
                               session_id=accepted["session"]["session_id"], prescription=accepted)
    resolution = resolve_rehab_completion(items, [injury], completion=completion)
    assert len(resolution.eligible) == 2
    prompts = build_rehab_response_prompts(resolution, [injury])
    assert len(prompts) == 1 and prompts[0].drill_ids == tuple(IDS)
    events = [build_rehab_exposure_event(
        candidate, athlete_id=ATHLETE, plan_id=PLAN, session_id=accepted["session"]["session_id"],
        training_day=DAY, completion=completion, during="same", limit="no",
    ) for candidate in resolution.eligible]
    assert len({e.exposure_id for e in events}) == 2
    assert len({e.response_group_id for e in events}) == 1
    assert all(e.is_attributable_to(injury) and e.dose_completed.completion_state == state for e in events)
    assert all(e.dose_completed.reps is None and e.dose_completed.sets is None for e in events)


@pytest.mark.parametrize("ids", [
    ["ankle_sprain_supported_balance", "not_reviewed"],
    ["ankle_sprain_supported_balance", "ankle_sprain_single_leg_balance_on_foam_pad"],
    ["ankle_sprain_supported_balance", "ankle_sprain_banded_ankle_circles"],
    ["ankle_sprain_supported_balance", "ankle_sprain_gentle_movement"],
    ["ankle_sprain_supported_balance", "ankle_sprain_supported_balance"],
])
def test_unreviewed_wrong_stage_or_duplicate_member_cannot_enter_bundle(ids):
    policy, _, _ = fixture(bundle=False)
    raw = policy.model_dump()
    raw.update(status="draft", activation="shadow", content_hash=None, stage_bundles={"restore": ids})
    with pytest.raises(ValidationError, match="bundle"):
        ClinicalPolicy.model_validate(raw)


@pytest.mark.parametrize("identity", IDS)
def test_stale_bank_member_and_excluded_member_fail_closed(identity):
    policy, injury, _ = fixture()
    bank = deepcopy(get_rehab_bank())
    member = next(d for g in bank for d in g["drills"] if d["id"] == identity)
    member["name"] = "Unreviewed change"
    result = resolve_injury_policy(injury, policies=(policy,), bank=bank)
    assert result["prescription"] is None
    result = resolve_injury_policy(injury, policies=(policy,), bank=get_rehab_bank(),
                                   excluded_drill_ids=[identity])
    assert result["prescription"] is None and result["reason_codes"] == ["reviewed_bundle_member_ineligible"]


def test_bundle_change_requires_a_new_policy_content_hash():
    policy, _, _ = fixture()
    with pytest.raises(ValidationError, match="content hash"):
        ClinicalPolicy.model_validate({**policy.model_dump(), "stage_bundles": {"restore": list(reversed(IDS))}})


def test_server_records_bundle_exposures_idempotently_from_one_injury_answer():
    _, injury, decision = fixture()
    store = FakeStore()
    store.injury_flags[ATHLETE] = [injury]
    accepted = snapshot([decision])
    completion = dict(plan_id=PLAN, status="done", rehab_performance="done_as_shown",
                      prescription_snapshot=accepted)
    kwargs = dict(athlete_id=ATHLETE, plan_row={"id": PLAN}, training_day=DAY,
                  session_id=accepted["session"]["session_id"], completion=completion,
                  answers={injury["id"]: dict(injury_episode_id=injury["episode_id"],
                                             during_response="same", limit_response="no")})
    recorded = record_rehab_exposures(store, **kwargs)
    retried = record_rehab_exposures(store, **kwargs)
    assert len(store.rehab_exposures) == 2
    assert [event.exposure_id for event in recorded] == [event.exposure_id for event in retried]
    assert len({event.response_group_id for event in recorded}) == 1


def test_today_daily_reservations_count_a_started_bundle_once(monkeypatch):
    from api.services import today_service
    policy, injury, decision = fixture()
    other = {**injury, "id": str(uuid4()), "episode_id": str(uuid4()), "body_area": "Right ankle", "side": "right"}
    store = FakeStore()
    store.injury_flags[ATHLETE] = [injury, other]
    store.upsert_session_completion(ATHLETE, dict(plan_id=PLAN, session_id=snapshot([decision])["session"]["session_id"],
        training_day=DAY, status="started", prescription_snapshot=snapshot([decision])))
    monkeypatch.setattr(today_service, "load_clinical_policies", lambda: (policy,))
    rows = today_service._with_injury_policy(
        [injury, other], store=store, athlete_id=ATHLETE, training_day=DAY,
        readiness_decision="train_as_planned", equipment=["stable_support"], training_session={"session_type": "strength"})
    states = {row["id"]: row["rehab_decision"]["schedule"]["state"] for row in rows}
    assert states == {injury["id"]: "already_completed", other["id"]: "due"}


@pytest.mark.parametrize("unknown_first", [True, False])
def test_one_member_delayed_answer_cannot_release_the_whole_bundle(unknown_first):
    _, injury, decision = fixture()
    responses = ["not_yet_known", "same"] if unknown_first else ["same", "not_yet_known"]
    events = [dict(athlete_id=ATHLETE, event_json=dict(
        injury_id=injury["id"], injury_episode_id=injury["episode_id"], drill_id=identity,
        occurred_at="2026-09-30T00:00:00Z", response_group_id="shared",
        response={"during_response": "same", "next_day_response": response}))
        for identity, response in zip(IDS, responses)]
    assert schedule_rehab(injury, decision, training_day=DAY, exposures=events)["state"] == "held"
    for event in events:
        event["event_json"]["response"]["next_day_response"] = "same"
    assert schedule_rehab(injury, decision, training_day=DAY, exposures=events)["state"] == "due"


@pytest.mark.parametrize("phase", ["GPP", "SPP", "TAPER"])
@pytest.mark.parametrize("severity", ["mild", "moderate"])
def test_live_ankle_restore_bundle_keeps_reviewed_bank_content_and_self_paced_doses(phase, severity):
    policy, injury, _ = fixture()
    bank = get_rehab_bank()
    decision = resolve_injury_policy({**injury, "severity": severity}, policies=load_clinical_policies(),
                                    bank=bank, phase=phase)
    assert policy.version == 7 and policy.stage_bundles == {"restore": IDS}
    assert policy.live_stages == ["calm", "restore", "load"]
    assert {t.to_stage for t in policy.transitions if t.promotable} == {"load"}
    assert policy.pathway_family == "ligament_sprain_or_instability"
    assert validate_clinical_bank((policy,), bank) == []
    assert policy_review_hash(policy) == policy.content_hash
    assert decision["stage"] == "restore" and decision["outcome"] == "prescribed_rehab"
    current = decision["prescription"]
    assert current["minimum_gap_days"] == 2 and current["is_loading"]
    assert [d["drill_id"] for d in current["drills"]] == IDS
    reviewed = {p.drill_id: p for p in policy.prescriptions}
    blocks = snapshot([decision])["session"]["blocks"]
    assert [b["rehab_drill_id"] for b in blocks] == IDS
    for member, block in zip(current["drills"], blocks):
        prescription = reviewed[member["drill_id"]]
        assert member["bank_hash"] == content_hash(member["drill"]) == prescription.bank_hash
        assert member["drill"]["rehab_stage"] == prescription.stage == "restore"
        assert block["instructions"] == prescription.instructions
        assert block["stop_rules"] == prescription.stop_when
        assert block["source_references"] == prescription.sources
        assert member["dose"] == block["dose"] == {}
        assert block["minimum_gap_days"] == 2


@pytest.mark.parametrize("reported,stage,ids", [
    ("ongoing", "calm", ["ankle_sprain_gentle_movement"]),
    ("improving", "restore", IDS),
    ("worse", "calm", ["ankle_sprain_gentle_movement"]),
])
def test_today_uses_live_bundle_only_for_injury_specific_restore_evidence(reported, stage, ids):
    from api.services import today_service
    _, injury, _ = fixture()
    injury.pop("rehab_stage")  # Today must derive the stage from the injury record.
    injury["latest_reported_status"] = reported
    injury["clinician_clearance"] = dict(episode_id=injury["episode_id"], scopes=["rehab", "training", "contact"])
    store = FakeStore()
    store.injury_flags[ATHLETE] = [injury]
    store.intakes[ATHLETE] = [dict(id=str(uuid4()), athlete_id=ATHLETE, equipment_access=["stable_support"])]
    store.plans[PLAN] = dict(id=PLAN, athlete_id=ATHLETE, intake_id=store.intakes[ATHLETE][0]["id"], status="ready", created_at="2026-09-01T00:00:00Z",
        structured_plan={"weeks": [{"phase_label": "TAPER", "days": [{"date": DAY, "day_type": "rest", "sessions": []}]}]})
    store.set_active_plan_id(ATHLETE, PLAN)
    store.upsert_today_checkin(ATHLETE, dict(plan_id=PLAN, training_day=DAY,
        recommendation_state="train_as_planned", pain="none", body="good"))
    view = today_service.build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC",
        now=datetime(2026, 10, 2, 12, tzinfo=timezone.utc))
    decision = view.open_injuries[0]["rehab_decision"]
    # #3003 synchronizes accessible rehab with the final session verdict;
    # the CALM prescription remains visible but cannot be due under a stop.
    expected_schedule = "held" if reported == "worse" else "due"
    assert decision["stage"] == stage and decision["schedule"]["state"] == expected_schedule
    live = view.live_prescription
    assert live and live["safety_hold"] is (reported == "worse")
    if reported == "worse":
        # Main already retains Today's stop guidance even though the CALM
        # movement remains visible. Publication must never bypass that hold.
        assert live["safety_hold_reason"] == "Follow today's stop guidance before training."
        assert decision["schedule"]["reason"] == live["safety_hold_reason"]
    assert [b["rehab_drill_id"] for b in live["session"]["blocks"]] == ids
    if stage == "calm":
        assert "drills" not in decision["prescription"]
        assert decision["prescription"]["minimum_gap_days"] == 1
        assert not decision["prescription"]["is_loading"]


@pytest.mark.parametrize("phase", ["GPP", "SPP", "TAPER"])
@pytest.mark.parametrize("stage,identity", [
    ("calm", "chest_strain_recovery_support"),
    ("restore", "chest_strain_comfortable_movement"),
])
def test_live_chest_remains_an_unchanged_single_drill_policy(stage, identity, phase):
    policy = next(p for p in load_clinical_policies() if p.policy_id == "chest_strain")
    _, injury, _ = fixture()
    injury.update(canonical_location="chest", body_region="chest", body_area="Chest",
                  side="unknown", injury_type="strain", rehab_stage=stage)
    assert policy.version == 3 and not policy.stage_bundles
    assert policy.content_hash == policy_review_hash(policy) == "bcb04950b08fa56d411e0117010d9dcc47c6057bc8fc84c2304fb746e20ba8d9"
    decision = resolve_injury_policy(injury, policies=load_clinical_policies(), bank=get_rehab_bank(), phase=phase)
    current = decision["prescription"]
    assert decision["stage"] == stage and current["drill_id"] == identity
    assert "drills" not in current and current["dose"] == {}
    assert current["minimum_gap_days"] == 1
    blocks = snapshot([decision])["session"]["blocks"]
    assert len(blocks) == 1 and blocks[0]["rehab_drill_id"] == identity
    assert "rehab_allocation_id" not in blocks[0]


@pytest.mark.parametrize("identity", IDS)
def test_live_bundle_uses_strictest_gap_after_work_on_either_member(identity):
    _, injury, decision = fixture()
    previous = snapshot([decision])
    previous["session"]["blocks"] = [b for b in previous["session"]["blocks"] if b["rehab_drill_id"] == identity]
    completion = dict(athlete_id=ATHLETE, training_day="2026-10-01", status="modified",
                      prescription_snapshot=previous)
    assert schedule_rehab(injury, decision, training_day=DAY, completions=[completion])["state"] == "recovery_day"
    assert schedule_rehab(injury, decision, training_day="2026-10-03", completions=[completion])["state"] == "due"


def test_today_does_not_split_a_live_bundle_to_bypass_training_or_readiness_gates():
    from api.services import today_service
    _, injury, _ = fixture()
    for context, state in [
        ({"readiness_decision": "pull_back"}, "due"),
        ({"readiness_decision": "not_checked_in"}, "held"),
        ({"readiness_decision": "stop"}, "held"),
        ({"training_session": {"blocks": [dict(block_type="strength", mechanical_load_regions=["ankle"], load="high")]}}, "deferred"),
    ]:
        rows = today_service._with_injury_policy([injury], store=FakeStore(), athlete_id=ATHLETE,
            training_day=DAY, equipment=["stable_support"], **context)
        decision = rows[0]["rehab_decision"]
        assert decision["schedule"]["state"] == state
        assert [d["drill_id"] for d in decision["prescription"]["drills"]] == IDS
        offered = snapshot([decision])
        if state == "due":
            assert [b["rehab_drill_id"] for b in offered["session"]["blocks"]] == IDS
        else:
            assert offered is None


@pytest.mark.parametrize("stage", ["load", "dynamic", "return"])
def test_live_bundle_never_enables_advanced_stages_even_with_clearance(stage):
    _, injury, _ = fixture()
    injury.update(rehab_stage=stage, clinician_clearance=dict(episode_id=injury["episode_id"],
                  scopes=["rehab", "training", "contact"]))
    decision = resolve_injury_policy(injury, policies=load_clinical_policies(), bank=get_rehab_bank())
    assert decision["stage"] == "calm"
    assert decision["prescription"]["drill_id"] == "ankle_sprain_gentle_movement"
    assert "drills" not in decision["prescription"]


def test_pilot_seed_reproduces_historical_baseline_without_changing_bank(tmp_path, monkeypatch):
    import json
    from tools import seed_rehab_pilot
    # Subtract the historical activation from a fixture, never the shared live
    # cache consumed by later pathway-safety and completion assertions.
    bank = deepcopy(get_rehab_bank())
    from tools.rehab_metadata_review_lib import before_achilles_load_activation, before_bank_publication
    bank = before_bank_publication(bank, "drills")
    # This seed owns the original v4 CALM/RESTORE baseline, not the later LOAD
    # activation. Subtract only the exact pinned addition before replaying it.
    from fightcamp.rehab_clinical import PATHWAYS_PATH
    manifest = json.loads((PATHWAYS_PATH.parent.parent / "tools/rehab_ankle_activation_inventory_baseline.json").read_text(encoding="utf-8"))
    group = next(g for g in bank if g["location"] == "ankle" and g["type"] == "sprain")
    assert manifest["new_drill"] in group["drills"]
    group["drills"].remove(manifest["new_drill"])
    data = tmp_path / "data"
    data.mkdir()
    (data / "rehab_bank.json").write_text(json.dumps(bank), encoding="utf-8")
    committed = json.dumps(before_achilles_load_activation(json.loads(PATHWAYS_PATH.read_text(encoding="utf-8"))),
        ensure_ascii=False, indent=2) + "\n"
    (data / "rehab_pathways.json").write_text(committed, encoding="utf-8")
    expected = load_clinical_policies(data / "rehab_pathways.json")
    monkeypatch.setattr(seed_rehab_pilot, "ROOT", tmp_path)
    seed_rehab_pilot.main()
    assert json.loads((data / "rehab_bank.json").read_text(encoding="utf-8")) == bank
    assert (data / "rehab_pathways.json").read_text(encoding="utf-8") == committed
    assert load_clinical_policies(data / "rehab_pathways.json") == expected
    live_group = next(g for g in get_rehab_bank() if g["location"] == "ankle" and g["type"] == "sprain")
    assert manifest["new_drill"] in live_group["drills"]
