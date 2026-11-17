"""Execution boundaries omitted by the existing clearance/ownership matrices."""
from copy import deepcopy

import pytest
from fastapi import HTTPException

from api.services import today_service
from tests.test_clinician_clearance_today import (
    CONTACT, REHAB, TRAIN, DAY, NOW, context as clearance_context, execute, live_injury, report,
    session, set_sessions, view,
)


@pytest.fixture
def context():
    return clearance_context.__wrapped__()


def contact_camp(context):
    live_injury(context, "ankle")
    set_sessions(context, [session("sparring", "Hard sparring", [dict(
        block_id="contact", block_type="sparring", display_name="Hard sparring",
        mechanical_load_regions=["ankle"], contact_level="full")])])
    report(context, CONTACT)


def update_checkin(context, fields):
    store, athlete, plan, _ = context
    row = store.get_today_checkin(athlete, plan, DAY)
    store.upsert_today_checkin(athlete, {**row, **fields})


@pytest.mark.parametrize("status", ["started", "done", "modified"])
def test_current_pullback_holds_frozen_contact_without_rewriting_acceptance(context, status):
    contact_camp(context)
    accepted = execute(context, view(context).live_prescription)
    snapshot = deepcopy(accepted["prescription_snapshot"])
    update_checkin(context, {"pain": "manageable"})
    current = view(context)
    live = current.live_prescription
    assert current.today.decision_tier == "pull_back"
    assert live["frozen"] and live["session"] == snapshot["session"]
    assert live["revision"] == snapshot["revision"] and live["safety_hold"]
    row = execute(context, live, status, session_rpe=5, modification_reason="Reduced rounds")
    assert row["status"] == status
    assert context[0].get_session_completion(context[1], "session-1", DAY)["prescription_snapshot"] == snapshot
    stopped = execute(context, live, "modified", modification_reason="Stopped on new readiness",
                      rehab_performance="stopped")
    assert stopped["prescription_snapshot"] == snapshot


@pytest.mark.parametrize("status", ["started", "done", "modified"])
def test_red_flag_stop_cannot_be_bypassed_by_support_completion(context, status):
    live_injury(context, "ankle")
    set_sessions(context, [session("mindset", "Tactical cue", [dict(
        block_id="cue", block_type="mindset", display_name="Tactical cue",
        mechanical_load_regions=[], contact_level="none")])])
    report(context, CONTACT)
    update_checkin(context, {"sharp_pain": True})
    assert view(context).today.decision_tier == "stop"
    row = today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
        payload=dict(plan_id=context[2], session_id="session-1", status=status,
                     session_rpe=1, modification_reason="Reduced cue"))
    assert row["status"] == status


@pytest.mark.parametrize("fields,tier", [({}, "green"), ({"sleep": "poor"}, "modify"),
    ({"pain": "manageable"}, "pull_back"), ({"sharp_pain": True}, "stop"), (None, "not_checked_in")])
def test_frozen_contact_readiness_equivalence_classes(context, fields, tier):
    contact_camp(context)
    accepted = execute(context, view(context).live_prescription)
    snapshot = deepcopy(accepted["prescription_snapshot"])
    if fields is None:
        context[0].today_checkins[context[1]] = []
    else:
        update_checkin(context, fields)
    current = view(context)
    live = current.live_prescription
    assert current.today.decision_tier == tier
    assert live["session"] == snapshot["session"] and live["revision"] == snapshot["revision"]
    assert live["safety_hold"] == (tier in {"pull_back", "stop", "not_checked_in"})
    if tier in {"green", "modify"}:
        execute(context, live, "done", session_rpe=5)


def test_no_checkin_warning_allows_uncatalogued_injury_logging(context):
    report(context, CONTACT)
    context[0].today_checkins[context[1]] = []
    current = view(context)
    assert current.today.decision_tier == "not_checked_in" and current.live_prescription is None
    row = today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
        payload=dict(plan_id=context[2], session_id="session-1", status="started"))
    assert row["status"] == "started"


@pytest.mark.parametrize("severity", ["mild", "moderate"])
@pytest.mark.parametrize("status", ["ongoing", "improving"])
def test_full_clearance_stable_and_improving_baselines_do_not_advance_rehab(context, severity, status):
    injury = live_injury(context, "ankle", severity)
    context[0].update_injury_flag(injury["id"], {"latest_reported_status": status})
    set_sessions(context, [session("strength", "Strength", [dict(
        block_id="work", block_type="strength", mechanical_load_regions=["ankle"], contact_level="none")])])
    before = view(context).open_injuries[0]["rehab_decision"]["stage"]
    report(context, CONTACT)
    current = view(context)
    assert current.open_injuries[0]["rehab_decision"]["stage"] == before
    assert not current.live_prescription["safety_hold"]
    assert any(b.get("block_id") == "work" for b in current.live_prescription["session"]["blocks"])


def test_pullback_preserves_independently_reviewed_nonloading_rehab(context):
    live_injury(context, "chest")
    report(context, CONTACT)
    update_checkin(context, {"sleep": "poor", "body": "flat", "pain": "manageable"})
    current = view(context)
    assert current.today.decision_tier == "pull_back"
    live = current.live_prescription
    assert live["rehab_only"] and not live["safety_hold"]
    assert all(b["block_type"] == "rehab" and not b["is_loading"] for b in live["session"]["blocks"])
    execute(context, live)
    execute(context, view(context).live_prescription, "done", rehab_performance="done_as_shown")


def test_severe_injury_support_exemption_survives_when_red_flags_are_clear(context):
    live_injury(context, "ankle", "severe")
    set_sessions(context, [session("mindset", "Tactical cue", [dict(
        block_id="cue", block_type="mindset", mechanical_load_regions=[], contact_level="none")])])
    report(context, CONTACT)
    current = view(context)
    assert current.today.injury_hold_exempt and current.today.decision_tier == "green"
    today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
        payload=dict(plan_id=context[2], session_id="session-1", status="done", session_rpe=1))


def test_known_combat_contact_cannot_use_full_clearance_to_override_poor_sleep(context):
    contact_camp(context)
    context[0].plans[context[2]]["technical_style"] = ["kickboxing"]
    context[0].plans[context[2]]["structured_plan"]["weeks"][0]["days"][0]["day_type"] = "hard"
    execute(context, view(context).live_prescription)
    update_checkin(context, {"sleep": "poor"})
    current = view(context)
    assert current.today.decision_tier == "pull_back" and current.live_prescription["safety_hold"]
    row = execute(context, current.live_prescription, "done", session_rpe=5)
    assert row["status"] == "done"


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("gate", [dict(severity="severe"), dict(latest_reported_status="worse"),
    dict(body_area="Ankle", description="Ankle fracture"), dict(body_area="Head", description="Concussion")])
def test_second_hard_injury_gate_wins_in_either_order(context, frozen, gate):
    contact_camp(context)
    accepted = execute(context, view(context).live_prescription) if frozen else None
    context[0].create_injury_flag(context[1], {"body_area": "Chest", "description": "Chest strain",
        "severity": "moderate", "status": "open", **gate})
    first = view(context)
    context[0].injury_flags[context[1]].reverse()
    second = view(context)
    assert first.today.decision_tier == second.today.decision_tier
    assert second.today.decision_tier in {"stop", "pull_back"}
    for current in (first, second):
        live = current.live_prescription
        assert live is None or live["safety_hold"]
        if accepted:
            assert live["session"] == accepted["prescription_snapshot"]["session"]
            assert live["revision"] == accepted["prescription_snapshot"]["revision"]
    payload = dict(plan_id=context[2], session_id="session-1", status="done", session_rpe=5)
    if frozen:
        row = today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW, payload=payload)
        assert row["prescription_snapshot"] == accepted["prescription_snapshot"]
    else:
        with pytest.raises(HTTPException, match="Refresh Today"):
            today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW, payload=payload)


@pytest.mark.parametrize("area,description", [("Ankle", "Ankle fracture"), ("Head", "Concussion")])
def test_medical_review_without_live_policy_still_blocks_noncontact_submission(context, area, description):
    injury = live_injury(context, "ankle")
    context[0].update_injury_flag(injury["id"], {"body_area": area, "description": description})
    set_sessions(context, [session("strength", "Light strength", [dict(
        block_id="light", block_type="strength", mechanical_load_regions=["shoulder"], contact_level="none", load="low")])])
    report(context, CONTACT)
    current = view(context)
    assert current.open_injuries[0]["rehab_decision"]["outcome"] == "medical_review"
    assert current.today.decision_tier in {"stop", "pull_back"} and current.live_prescription is None
    row = today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
        payload=dict(plan_id=context[2], session_id="session-1", status="started"))
    assert row["status"] == "started"


@pytest.mark.parametrize("scopes", [REHAB, TRAIN])
@pytest.mark.parametrize("coached_contact", [False, True])
@pytest.mark.parametrize("watch_first", [False, True])
def test_clearance_preserves_zero_load_tactical_watch_without_claiming_contact(context, scopes, coached_contact, watch_first):
    live_injury(context, "ankle")
    watch = session("skill", "Tactical Watch", [dict(block_id="watch", block_type="mindset",
        display_name="Sparring exchange review", coaching_cues=["Watch how the supporting leg reacts to low kicks."])])
    set_sessions(context, [watch])
    if coached_contact:
        day = context[0].plans[context[2]]["structured_plan"]["weeks"][0]["days"][0]
        day["today_card"] = {"coach_led_contact": "Hard sparring"}
        day["sessions"].append(dict(session_id="contact-owned", session_type="sparring", title="Hard sparring", blocks=[]))
        if not watch_first:
            day["sessions"].reverse()
    report(context, scopes)
    current = view(context)
    live = current.live_prescription
    assert live and not live["safety_hold"] and not live["rehab_only"]
    assert live["session"]["session_id"] == "session-1"
    assert any(b.get("block_id") == "watch" and not b.get("_policy_held") for b in live["session"]["blocks"])
    assert not live["session"].get("coach_led_contact")
    execute(context, live)
    resumed = view(context).live_prescription
    assert resumed["session"] == live["session"] and resumed["revision"] == live["revision"]
    execute(context, resumed, "done", session_rpe=1,
            rehab_performance="done_as_shown" if any(b.get("block_type") == "rehab" for b in live["session"]["blocks"]) else None)
    assert context[0].get_session_completion(context[1], "contact-owned", DAY) is None
    if coached_contact:
        report(context, CONTACT, stamp="2026-09-30T11:00:00Z")
        remaining = view(context).live_prescription
        assert remaining and remaining["session"]["session_id"] == "contact-owned"
        assert not remaining["safety_hold"]


@pytest.mark.parametrize("region", ["shoulder", "ankle"])
def test_rehab_clearance_keeps_safe_mobility_but_not_unreviewed_injured_region_load(context, region):
    live_injury(context, "ankle")
    set_sessions(context, [session("recovery", "Downshift mobility", [dict(
        block_id="mobility", block_type="accessory", display_name="Gentle mobility",
        mechanical_load_regions=[region], contact_level="none")])])
    report(context, REHAB)
    live = view(context).live_prescription
    if region == "shoulder":
        assert not live["safety_hold"] and not live["rehab_only"]
        assert any(b.get("block_id") == "mobility" for b in live["session"]["blocks"])
        execute(context, live)
    else:
        assert live["safety_hold"] or live["rehab_only"]
        assert not any(b.get("block_id") == "mobility" and not b.get("_policy_held") for b in live["session"]["blocks"])


@pytest.mark.parametrize("unsafe", [{"mechanical_load_regions": ["ankle"]}, {"contact_level": "full"}])
def test_mindset_label_cannot_hide_explicit_load_or_contact(context, unsafe):
    live_injury(context, "ankle")
    set_sessions(context, [session("skill", "Tactical Watch", [dict(
        block_id="unsafe", block_type="mindset", display_name="Actual partner practice", **unsafe)])])
    report(context, REHAB)
    live = view(context).live_prescription
    assert live["safety_hold"] or live["rehab_only"]
    assert not any(b.get("block_id") == "unsafe" and not b.get("_policy_held") for b in live["session"]["blocks"])


def test_zero_load_contact_companion_keeps_original_one_allocation_ceiling(context):
    injury = live_injury(context, "ankle")
    context[0].update_injury_flag(injury["id"], {"created_at": DAY, "latest_reported_status": "ongoing"})
    context[0].create_injury_flag(context[1], dict(body_area="Chest", description="Chest strain", severity="mild", status="open"))
    set_sessions(context, [session("skill", "Tactical Watch", [dict(block_id="watch", block_type="mindset", display_name="Watch film")]),
        dict(session_id="contact-owned", session_type="sparring", title="Hard sparring", blocks=[])])
    report(context, REHAB)
    current = view(context)
    live = current.live_prescription
    from api.contracts.injury_policy import rehab_allocation_count
    assert live and not live["safety_hold"] and live["allocation_limit"] == 1
    assert rehab_allocation_count(live["session"]["blocks"]) == 1
    assert any(row["rehab_decision"]["schedule"]["state"] == "deferred" for row in current.open_injuries)
    execute(context, live)
    execute(context, view(context).live_prescription, "done", session_rpe=1, rehab_performance="done_as_shown")
    assert context[0].get_session_completion(context[1], "contact-owned", DAY) is None


def test_zero_load_support_still_holds_under_a_current_red_flag(context):
    live_injury(context, "ankle")
    set_sessions(context, [session("skill", "Tactical Watch", [dict(block_id="watch", block_type="mindset", display_name="Watch sparring")])])
    report(context, REHAB)
    update_checkin(context, {"sharp_pain": True})
    current = view(context)
    assert current.today.decision_tier == "stop"
    assert current.live_prescription is None or current.live_prescription["safety_hold"]


def test_zero_load_support_does_not_need_clearance_to_watch_a_combat_topic(context):
    live_injury(context, "ankle")
    set_sessions(context, [session("skill", "Tactical Watch", [dict(
        block_id="watch", block_type="mindset", display_name="Sparring exchange review")])])
    live = view(context).live_prescription
    assert live and not live["safety_hold"]
    assert any(block.get("block_id") == "watch" for block in live["session"]["blocks"])


def test_support_projection_without_a_live_policy_preserves_contact_ownership(context):
    set_sessions(context, [session("skill", "Tactical Watch", [dict(
        block_id="watch", block_type="mindset", display_name="Watch film")]),
        dict(session_id="contact-owned", session_type="sparring", title="Hard sparring", blocks=[])])
    report(context, REHAB)
    live = view(context).live_prescription
    assert live and not live["safety_hold"]
    execute(context, live)
    execute(context, view(context).live_prescription, "done", session_rpe=1)
    assert context[0].get_session_completion(context[1], "contact-owned", DAY) is None


@pytest.mark.parametrize("frozen", [False, True])
def test_rehab_session_label_cannot_exempt_ordinary_camp_from_pullback(context, frozen):
    live_injury(context, "ankle")
    set_sessions(context, [session("rehab", "Rehab-labelled camp", [dict(
        block_id="work", block_type="strength", display_name="Light strength",
        mechanical_load_regions=["shoulder"], contact_level="none")])])
    report(context, CONTACT)
    accepted = execute(context, view(context).live_prescription) if frozen else None
    update_checkin(context, {"pain": "manageable", "sleep": "poor", "body": "flat"})
    current = view(context)
    assert current.today.decision_tier == "pull_back"
    live = current.live_prescription
    assert not live["rehab_only"] and live["safety_hold"]
    if accepted:
        assert live["session"] == accepted["prescription_snapshot"]["session"]
    with pytest.raises(HTTPException):
        execute(context, live, "done", session_rpe=2)


@pytest.mark.parametrize("field", ["title", "label", "name", "athlete_facing_label", "display_name"])
def test_mindset_type_does_not_erase_loaded_movement_names(field):
    from api.contracts.readiness_message import _is_non_physical_mapping, _mapping_executable_text, is_support_session
    block = {"block_type": "mindset", field: "Heavy squat reps"}
    assert not _is_non_physical_mapping(block)
    assert "heavy squat reps" in _mapping_executable_text(block)
    assert not is_support_session(session("skill", "Tactical Watch", [block]))


def test_cognitive_children_cannot_override_a_high_risk_session_title():
    from api.contracts.readiness_message import is_support_session
    assert not is_support_session(session("skill", "Heavy squat day", [dict(
        block_type="mindset", display_name="Watch film")]))


@pytest.mark.parametrize("cognitive", [False, True])
def test_unknown_active_injury_region_blocks_physical_support_only(context, cognitive):
    injury = live_injury(context, "ankle")
    context[0].update_injury_flag(injury["id"], {"body_area": "Other", "description": "Unspecified soreness"})
    support = session("skill", "Tactical Watch", [dict(block_id="support", block_type="mindset", display_name="Watch film")]) if cognitive else session(
        "recovery", "Downshift mobility", [dict(block_id="support", block_type="accessory",
            display_name="Gentle mobility", mechanical_load_regions=["shoulder"], contact_level="none")])
    set_sessions(context, [support, dict(session_id="contact-owned", session_type="sparring", title="Hard sparring", blocks=[])])
    report(context, REHAB)
    current = view(context)
    assert any(not row.get("canonical_location") for row in current.open_injuries)
    live = current.live_prescription
    if cognitive:
        assert live and not live["safety_hold"]
        assert any(block.get("block_id") == "support" for block in live["session"]["blocks"])
        execute(context, live)
    else:
        assert live and live["safety_hold"]


def test_mindset_loaded_name_cannot_survive_live_policy_reconciliation(context):
    live_injury(context, "ankle")
    set_sessions(context, [session("skill", "Tactical Watch", [dict(
        block_id="unsafe", block_type="mindset", display_name="Heavy squat reps")])])
    report(context, REHAB)
    live = view(context).live_prescription
    assert live and (live["safety_hold"] or live["rehab_only"])
    assert not any(block.get("block_id") == "unsafe" and not block.get("_policy_held") for block in live["session"]["blocks"])


def test_loaded_mindset_cannot_use_the_no_live_severe_injury_exemption(context):
    context[0].update_injury_flag(context[3]["id"], {"severity": "severe"})
    set_sessions(context, [session("skill", "Tactical Watch", [dict(
        block_type="mindset", display_name="Heavy squat reps")])])
    report(context, REHAB)
    current = view(context)
    assert current.today.decision_tier == "stop" and not current.today.injury_hold_exempt
    with pytest.raises(HTTPException):
        today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
            payload=dict(plan_id=context[2], session_id="session-1", status="started"))
