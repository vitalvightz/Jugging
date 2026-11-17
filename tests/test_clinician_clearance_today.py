"""Episode reports constrain execution, never supply rehab recovery evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.contracts.injury_policy import reconcile_session_prescription, resolve_injury_policy
from api.contracts.rehab_progression import resolve_reviewed_progression
from api.services.injury_episode_service import (
    InjuryEpisodeObservation, apply_episode_observations, record_episode_observation,
)
from api.services import today_service
from fightcamp.rehab_clinical import load_clinical_policies
from fightcamp.rehab_protocols import get_rehab_bank
from tests.support import FakeStore

DAY = "2026-09-30"
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
REHAB = ["rehab"]
TRAIN = ["rehab", "training"]
CONTACT = ["rehab", "training", "contact"]


def session(kind="strength", title="Strength", blocks=None):
    return dict(session_id="session-1", session_type=kind, title=title, blocks=blocks if blocks is not None else [
        dict(block_id="block-1", block_type=kind, display_name=title, mechanical_load_regions=["shoulder"], contact_level="none")])


@pytest.fixture
def context():
    store = FakeStore()
    athlete, plan = str(uuid4()), str(uuid4())
    store.plans[plan] = dict(id=plan, athlete_id=athlete, status="ready", created_at="2026-09-01T00:00:00Z",
        structured_plan={"weeks": [{"phase_label": "GPP", "days": [{"date": DAY, "day_type": "strength", "sessions": [session()]}]}]})
    store.set_active_plan_id(athlete, plan)
    store.upsert_today_checkin(athlete, dict(plan_id=plan, training_day=DAY, recommendation_state="train_as_planned", pain="none", body="good"))
    injury = store.create_injury_flag(athlete, dict(body_area="Elbow", description="Elbow strain", severity="mild", status="open"))
    return store, athlete, plan, injury


def report(context, scopes, stamp="2026-09-29T12:00:00Z", injury=None):
    store, athlete, _, original = context
    injury = injury or original
    event = record_episode_observation(store, athlete_id=athlete, training_day=DAY, observation=InjuryEpisodeObservation(
        injury_id=injury["id"], injury_episode_id=injury["episode_id"], event_type="clinician_clearance_report", scopes=scopes))
    store.injury_episode_events[event["id"]]["created_at"] = stamp
    return store.injury_episode_events[event["id"]]


def view(context):
    return today_service.build_today_command_view(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW)


def set_sessions(context, sessions):
    context[0].plans[context[2]]["structured_plan"]["weeks"][0]["days"][0]["sessions"] = sessions


def execute(context, live, status="started", **extra):
    return today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
        payload=dict(plan_id=context[2], session_id=live["session"]["session_id"], status=status, prescription_revision=live["revision"], **extra))


def test_rehab_only_holds_normal_training_without_a_live_rehab_policy(context):
    report(context, REHAB)
    current = view(context)
    assert current.open_injuries[0]["rehab_decision"]["activation"] == "shadow"
    live = current.live_prescription
    assert live["safety_hold"] and "rehab only" in live["safety_hold_reason"]
    row = execute(context, live)
    assert row["status"] == "started"


@pytest.mark.parametrize("entry", [
    session("sparring", "Hard sparring", []),
    session("hard_sparring", "Hard sparring", [dict(block_type="rounds", display_name="Rounds")]),
    session("strength", "Strength", [dict(block_type="contact", display_name="Contact sparring", contact_level="full")]),
    {**session(), "coach_led_contact": "Hard sparring"},
])
def test_no_contact_holds_all_clear_contact_representations(context, entry):
    set_sessions(context, [entry])
    report(context, TRAIN)
    live = view(context).live_prescription
    assert live["safety_hold"] and "excludes hard sparring" in live["safety_hold_reason"]
    row = execute(context, live)
    assert row["status"] == "started"


@pytest.mark.parametrize("entry", [session(), session("technical", "Non-contact shadowboxing", []),
                                   session("technical", "Technical grappling", []),
                                   session("sparring", "Technical sparring", [])])
def test_no_contact_keeps_otherwise_safe_noncontact_training(context, entry):
    set_sessions(context, [entry])
    report(context, TRAIN)
    assert view(context).live_prescription is None
    row = today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
        payload=dict(plan_id=context[2], session_id="session-1", status="started"))
    assert row["status"] == "started"


def test_no_contact_checks_a_blockless_contact_sibling(context):
    set_sessions(context, [session(), dict(session_id="contact-sibling", session_type="grappling", title="Live grappling", blocks=[])])
    report(context, TRAIN)
    assert view(context).live_prescription["safety_hold"]


@pytest.mark.parametrize("scopes", [TRAIN, CONTACT])
def test_permissive_scope_does_not_override_severe_injury(context, scopes):
    set_sessions(context, [session("sparring", "Hard sparring", [])])
    report(context, scopes)
    context[0].update_injury_flag(context[3]["id"], dict(severity="severe"))
    current = view(context)
    assert current.today.decision_tier == "stop"
    if scopes == TRAIN:
        assert current.live_prescription["safety_hold"]
    else:
        assert current.live_prescription is None
    assert current.open_injuries[0]["rehab_decision"]["outcome"] == "medical_review"
    payload = dict(plan_id=context[2], session_id="session-1", status="started")
    if current.live_prescription:
        payload["prescription_revision"] = current.live_prescription["revision"]
    row = today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
        payload=payload)
    assert row["status"] == "started"


@pytest.mark.parametrize("scopes", [REHAB, TRAIN, CONTACT])
def test_clearance_does_not_advance_stages_or_clear_setbacks(scopes):
    injury = dict(id="injury", episode_id="episode", athlete_id="athlete", body_area="Chest", description="Chest strain",
        canonical_location="chest", injury_type="strain", severity="mild", status="open", latest_reported_status="worse",
        updated_at="2026-09-29T12:00:00Z", rehab_stage="restore",
        clinician_clearance=dict(episode_id="episode", scopes=scopes, source="athlete_reported", externally_verified=False))
    policy = next(p for p in load_clinical_policies() if p.region == "chest")
    assert resolve_reviewed_progression(injury, base_stage="restore", policy=policy, exposures=())["stage"] == "calm"
    decision = resolve_injury_policy(injury, policies=load_clinical_policies(), bank=get_rehab_bank())
    assert decision["stage"] == "calm"
    assert injury["latest_reported_status"] == "worse" and injury["status"] == "open"


def test_latest_valid_report_replaces_historical_scopes_in_both_directions(context):
    first = report(context, CONTACT)
    second = report(context, TRAIN, "2026-09-30T10:00:00Z")
    for observations in ([first, second], [second, first]):
        effective = apply_episode_observations(context[3], observations)["clinician_clearance"]
        assert effective["scopes"] == sorted(TRAIN)
        assert effective["source"] == "athlete_reported" and effective["externally_verified"] is False
        assert set(effective["scope_reported_at"].values()) == {second["created_at"]}
    third = report(context, CONTACT, "2026-09-30T11:00:00Z")
    assert apply_episode_observations(context[3], [third, first, second])["clinician_clearance"]["scopes"] == sorted(CONTACT)
    fourth = report(context, REHAB, "2026-09-30T11:30:00Z")
    assert apply_episode_observations(context[3], [first, second, third, fourth])["clinician_clearance"]["scopes"] == REHAB
    assert context[0].get_injury_flag_for_athlete(context[3]["id"], context[1])["status"] == "open"


@pytest.mark.parametrize("kind", ["injury_checkin", "delayed_rehab_response"])
def test_later_setback_keeps_reported_clearance(context, kind):
    event = report(context, CONTACT)
    derived = apply_episode_observations(context[3], [event])
    setback = dict(athlete_id=context[1], injury_id=context[3]["id"], injury_episode_id=context[3]["episode_id"],
        event_type=kind, created_at="2026-09-30T10:00:00Z",
        payload={"latest_reported_status": "worse"} if kind == "injury_checkin" else {"response": "worse"})
    # A clearance stays until the athlete changes it; Worse only pauses rehab.
    assert apply_episode_observations(derived, [event, setback])["clinician_clearance"]["scopes"] == sorted(CONTACT)
    fresh = report(context, TRAIN, "2026-09-30T11:00:00Z")
    assert apply_episode_observations(derived, [event, setback, fresh])["clinician_clearance"]["scopes"] == sorted(TRAIN)


def test_multiple_injuries_use_the_most_restrictive_current_ceiling(context):
    set_sessions(context, [session("sparring", "Hard sparring", [])])
    other = context[0].create_injury_flag(context[1], dict(body_area="Wrist", description="Wrist strain", severity="mild", status="open"))
    report(context, CONTACT)
    report(context, TRAIN, injury=other)
    assert view(context).live_prescription["safety_hold"]
    report(context, CONTACT, "2026-09-30T10:00:00Z", injury=other)
    assert view(context).live_prescription is None


def test_new_restrictive_report_holds_frozen_work_without_rewriting_it(context):
    set_sessions(context, [session("sparring", "Hard sparring", [])])
    # A prescription accepted under the existing architecture remains immutable.
    frozen = dict(plan_id=context[2], training_day=DAY, session=session("sparring", "Hard sparring", []),
        safety_hold=False, frozen=False, changes=[], revision="a" * 64, injury_ids=[context[3]["id"]],
        injury_context=[dict(id=context[3]["id"])])
    context[0].upsert_session_completion(context[1], dict(plan_id=context[2], session_id="session-1", training_day=DAY,
        status="started", prescription_snapshot=deepcopy(frozen), started_at="2026-09-30T09:00:00Z"))
    report(context, TRAIN)
    live = view(context).live_prescription
    assert live["frozen"] and live["safety_hold"]
    assert live["session"] == frozen["session"] and live["revision"] == frozen["revision"]
    row = execute(context, live, "done", session_rpe=5)
    assert row["status"] == "done"
    stopped = execute(context, live, "modified", modification_reason="Stopped on updated guidance", rehab_performance="stopped")
    assert stopped["prescription_snapshot"] == frozen
    assert stopped["rehab_performance"] == "stopped"


def test_new_ceiling_on_legacy_started_work_uses_existing_hold(context):
    context[0].upsert_session_completion(context[1], dict(plan_id=context[2], session_id="session-1", training_day=DAY,
        status="started", started_at="2026-09-30T09:00:00Z"))
    report(context, REHAB)
    live = view(context).live_prescription
    assert live["safety_hold"] and live["session"]["session_id"] == "session-1"
    assert "started before" in live["safety_hold_reason"]


@pytest.mark.parametrize("scopes", [REHAB, TRAIN, CONTACT])
@pytest.mark.parametrize("decision", ["pull_back", "stop", "not_checked_in"])
def test_clearance_cannot_remove_a_reviewed_rehab_readiness_hold(scopes, decision):
    injury = dict(id="chest", episode_id="episode", athlete_id="athlete", body_area="Chest", description="Chest strain",
        canonical_location="chest", injury_type="strain", severity="mild", status="open",
        clinician_clearance=dict(episode_id="episode", scopes=scopes))
    current = resolve_injury_policy(injury, policies=load_clinical_policies(), bank=get_rehab_bank(), readiness_decision=decision)
    from api.contracts.rehab_schedule import schedule_rehab
    current["schedule"] = schedule_rehab(injury, current, training_day=DAY, readiness_decision=decision)
    if decision in {"stop", "not_checked_in"}:
        assert current["schedule"]["state"] == "held"
    else:
        assert not current["prescription"]["is_loading"]


def test_rehab_only_keeps_independently_eligible_reviewed_rehab(context):
    store, athlete, _, injury = context
    store.update_injury_flag(injury["id"], dict(body_area="Chest", description="Chest strain", body_region="chest"))
    report(context, REHAB)
    current = view(context)
    live = current.live_prescription
    assert live["rehab_only"] and not live["safety_hold"]
    assert live["session"]["session_type"] == "rehab" and live["session"]["session_id"].startswith(f"rehab-{DAY}-")
    assert all(b["block_type"] == "rehab" for b in live["session"]["blocks"])
    execute(context, live)
    assert not view(context).live_prescription["safety_hold"]


def test_permissive_clearance_does_not_remove_a_preexisting_frozen_hold(context):
    injury = {**context[3], "clinician_clearance": dict(episode_id=context[3]["episode_id"], scopes=CONTACT)}
    frozen = dict(session=session(), safety_hold=True, revision="a" * 64)
    result = reconcile_session_prescription(None, decisions=[], plan_id=context[2], training_day=DAY, frozen=frozen, injuries=[injury])
    assert result["safety_hold"] and result["session"] == frozen["session"]


def test_clearance_report_is_part_of_the_existing_prescription_revision(context):
    event = report(context, TRAIN)
    assert context[0].get_rehab_schedule_revision(context[1])["event_id"] == event["id"]


def test_another_injurys_restriction_wins_over_full_contact_clearance(context):
    report(context, CONTACT)
    context[0].create_injury_flag(context[1], dict(body_area="Neck", description="Neck injury", severity="severe", status="open"))
    current = view(context)
    assert current.today.decision_tier == "stop"
    row = today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
        payload=dict(plan_id=context[2], session_id="session-1", status="started"))
    assert row["status"] == "started"


def test_clearance_does_not_replace_a_stronger_frozen_hold_explanation(context):
    injury = {**context[3], "clinician_clearance": dict(episode_id=context[3]["episode_id"], scopes=REHAB)}
    frozen = dict(session=session(), safety_hold=True, safety_hold_reason="Medical hold", revision="a" * 64)
    result = reconcile_session_prescription(None, decisions=[], plan_id=context[2], training_day=DAY, frozen=frozen, injuries=[injury])
    assert result["safety_hold"] and result["safety_hold_reason"] == "Medical hold"


@pytest.mark.parametrize("replacement", [False, True])
def test_no_contact_preserves_safe_blocks_in_a_mixed_session(context, replacement):
    safe = session()["blocks"][0]
    contact = dict(block_id="contact", block_type="sparring", display_name="Hard sparring",
                   mechanical_load_regions=["shoulder"], contact_level="full", role="skill", dose={"minutes": 10})
    alternate = dict(block_type="technical", display_name="Non-contact shadowboxing", mechanical_load_regions=["shoulder"],
                     contact_level="none", role="skill", dose={"minutes": 10})
    if replacement:
        contact["alternates"] = [alternate]
    set_sessions(context, [session("mixed", "Mixed training", [safe, contact])])
    report(context, TRAIN)
    live = view(context).live_prescription
    assert not live["safety_hold"]
    assert live["session"]["blocks"][0] == safe
    assert not any(b.get("contact_level") == "full" for b in live["session"]["blocks"])
    assert len(live["session"]["blocks"]) == (2 if replacement else 1)
    assert any(c.get("block_id") == "contact" and c["action"] == ("substituted" if replacement else "removed")
               for c in live["changes"])
    execute(context, live)


def test_no_contact_does_not_remove_stricter_block_restrictions(context):
    injury = {**context[3], "clinician_clearance": dict(episode_id=context[3]["episode_id"], scopes=TRAIN)}
    entry = session("mixed", "Mixed training", [session()["blocks"][0],
        dict(block_id="contact", block_type="sparring", contact_level="full", mechanical_load_regions=["shoulder"])])
    decisions = [dict(activation="live", injury_id=injury["id"], outcome="held", restrictions={
        "blocked_regions": ["shoulder"], "blocked_tags": [], "contact_limit": "none"})]
    result = reconcile_session_prescription(entry, decisions=decisions, plan_id=context[2], training_day=DAY, injuries=[injury])
    assert result["safety_hold"]
    assert all(b["_policy_held"] for b in result["session"]["blocks"])


def live_injury(context, region, severity="moderate"):
    store, athlete, _, injury = context
    # Published ankle RESTORE bundles require the athlete's declared support.
    intake_id = str(uuid4())
    store.intakes[athlete] = [dict(id=intake_id, athlete_id=athlete,
        equipment_access=["stable_support", "chair", "wall", "table"])]
    store.plans[context[2]]["intake_id"] = intake_id
    fields = dict(body_area="Right ankle" if region == "ankle" else "Chest",
                  description=region + (" sprain" if region == "ankle" else " strain"),
                  body_region=region, side="right" if region == "ankle" else "unknown",
                  severity=severity, status="monitoring", latest_reported_status="improving",
                  created_at="2026-09-27T00:00:00Z", updated_at="2026-09-29T09:00:00Z")
    store.update_injury_flag(injury["id"], fields)
    return store.get_injury_flag_for_athlete(injury["id"], athlete)


def live_camp(region, contact=True):
    return session("technical", "Low-kick technical session", [
        dict(block_id="skill", block_type="technical", display_name="Low-kick counter",
             mechanical_load_regions=[region], contact_level="none"),
        *([dict(block_id="contact", block_type="sparring", display_name="Hard sparring",
                mechanical_load_regions=[region], contact_level="full")] if contact else []),
        dict(block_id="mindset", block_type="mindset", display_name="Low-kick counter decision",
             mechanical_load_regions=[], contact_level="none"),
    ])


@pytest.mark.parametrize("region", ["ankle", "chest"])
@pytest.mark.parametrize("scopes", [REHAB, TRAIN, CONTACT])
@pytest.mark.parametrize("completed_rehab", [False, True])
@pytest.mark.parametrize("severity", ["mild", "moderate"])
def test_live_clearance_relaxes_only_cleared_baseline_with_or_without_due_rehab(context, region, scopes, completed_rehab, severity):
    injury = live_injury(context, region, severity)
    if completed_rehab:
        current = resolve_injury_policy({**injury, "rehab_stage": "restore"}, policies=load_clinical_policies(), bank=get_rehab_bank())
        saved = reconcile_session_prescription(None, decisions=[current], plan_id=context[2], training_day=DAY)
        context[0].upsert_session_completion(context[1], dict(plan_id=context[2], session_id=saved["session"]["session_id"],
            training_day=DAY, status="done", prescription_snapshot=saved, rehab_performance="done_as_shown"))
    set_sessions(context, [live_camp(region)])
    report(context, scopes)
    before = deepcopy(context[0].plans[context[2]])
    current = view(context)
    live = current.live_prescription
    assert current.open_injuries[0]["rehab_decision"]["stage"] in {"calm", "restore"}
    assert current.open_injuries[0]["rehab_decision"]["restrictions"]["contact_limit"] == "none"
    assert context[0].plans[context[2]] == before
    if scopes == REHAB:
        due = current.open_injuries[0]["rehab_decision"]["schedule"]["state"] == "due"
        assert live["rehab_only"] if due else live["safety_hold"]
        return
    assert not live["safety_hold"] and not live["rehab_only"]
    blocks = live["session"]["blocks"]
    assert any(b.get("block_id") == "skill" for b in blocks)
    assert any(b.get("block_id") == "mindset" for b in blocks)
    assert any(b.get("block_id") == "contact" for b in blocks) == (scopes == CONTACT)
    due = current.open_injuries[0]["rehab_decision"]["schedule"]["state"] == "due"
    assert any(b.get("block_type") == "rehab" for b in blocks) == due
    if region == "ankle" and not completed_rehab:
        assert current.open_injuries[0]["rehab_decision"]["schedule"]["state"] == "deferred"
    assert not any(b.get("_policy_held") for b in blocks)
    execute(context, live)
    frozen = view(context).live_prescription
    assert frozen["frozen"] and frozen["session"] == live["session"] and not frozen["safety_hold"]
    execute(context, frozen, "done", rehab_performance="done_as_shown" if due else None)


@pytest.mark.parametrize("region", ["ankle", "chest"])
def test_live_full_clearance_allows_contact_owned_day_without_app_blocks(context, region):
    live_injury(context, region)
    set_sessions(context, [session("sparring", "Hard sparring", [])])
    report(context, CONTACT)
    live = view(context).live_prescription
    assert not live["safety_hold"] and not live["rehab_only"]
    assert live["session"]["session_id"] == "session-1"
    execute(context, live)


@pytest.mark.parametrize("scopes", [None, TRAIN, CONTACT])
@pytest.mark.parametrize("headline", ["Light Combat / Technical", "Technical sparring", "Hard sparring"])
def test_training_clearance_allows_technical_but_not_hard_headline_only_day(context, scopes, headline):
    # Contact clearance is for hard sparring; training clearance covers technical work.
    live_injury(context, "ankle")
    day = context[0].plans[context[2]]["structured_plan"]["weeks"][0]["days"][0]
    day.update(day_type="rest", sessions=[], today_card={"headline": headline})
    if scopes:
        report(context, scopes)
    live = view(context).live_prescription
    allowed = scopes == CONTACT or (scopes == TRAIN and headline != "Hard sparring")
    assert (live["safety_hold"] or live["rehab_only"]) != allowed
    if allowed:
        assert live["session"]["session_id"] == DAY
        execute(context, live)


def test_training_clearance_keeps_technical_blocks_and_removes_hard_sparring(context):
    live_injury(context, "ankle")
    set_sessions(context, [session("strength", "Strength", [
        dict(block_id="squat", block_type="strength", mechanical_load_regions=["ankle"], contact_level="none"),
        dict(block_id="positional", block_type="sparring", display_name="Positional sparring"),
        dict(block_id="hard", block_type="sparring", display_name="Hard sparring")])])
    report(context, TRAIN)
    live = view(context).live_prescription
    assert not live["safety_hold"]
    kept = {b.get("block_id") for b in live["session"]["blocks"]}
    assert {"squat", "positional"} <= kept and "hard" not in kept


@pytest.mark.parametrize("flag", ["sharp_pain", "instability", "swelling", "neurological_symptoms", "illness_symptoms",
                                 "cannot_warm_into_movement", "worse_next_day_pain"])
@pytest.mark.parametrize("completion_status", ["started", "done", "modified"])
def test_live_full_clearance_never_overrides_a_current_red_flag(context, flag, completion_status):
    live_injury(context, "ankle")
    set_sessions(context, [live_camp("ankle")])
    report(context, CONTACT)
    row = context[0].get_today_checkin(context[1], context[2], DAY)
    context[0].upsert_today_checkin(context[1], {**row, flag: True})
    current = view(context)
    assert current.today.decision_tier == "stop"
    assert current.live_prescription is None or current.live_prescription["safety_hold"]
    row = today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
        payload=dict(plan_id=context[2], session_id="session-1", status=completion_status, modification_reason="Stopped"))
    assert row["status"] == completion_status


@pytest.mark.parametrize("change", [dict(severity="severe"), dict(latest_reported_status="worse"),
                                    dict(body_area="Neck", description="Concussion", body_region="neck")])
def test_live_full_clearance_cannot_relax_hard_injury_gates(context, change):
    injury = live_injury(context, "ankle")
    set_sessions(context, [live_camp("ankle")])
    report(context, CONTACT)
    context[0].update_injury_flag(injury["id"], {**change, "updated_at": "2026-09-30T10:00:00Z"})
    current = view(context)
    assert current.today.decision_tier in {"stop", "pull_back"}
    assert current.live_prescription is None or current.live_prescription["safety_hold"] or current.live_prescription["rehab_only"]
    payload = dict(plan_id=context[2], session_id="session-1", status="started")
    if change.get("latest_reported_status") == "worse":
        with pytest.raises(HTTPException, match="use the current session"):
            today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW, payload=payload)
    else:
        row = today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW, payload=payload)
        assert row["status"] == "started"


@pytest.mark.parametrize("scopes", [None, ["rehab", "contact"], CONTACT])
def test_clearance_cannot_relax_another_uncleared_or_malformed_episode(context, scopes):
    live_injury(context, "ankle")
    set_sessions(context, [live_camp("ankle")])
    report(context, CONTACT)
    other = context[0].create_injury_flag(context[1], dict(body_area="Chest", description="Chest strain",
        body_region="chest", severity="moderate", status="open"))
    if scopes is not None:
        event = report(context, CONTACT, injury=other)
        context[0].injury_episode_events[event["id"]]["payload"]["scopes"] = scopes
    current = view(context)
    if scopes == CONTACT:
        assert not current.live_prescription["safety_hold"] and not current.live_prescription["rehab_only"]
    else:
        assert current.live_prescription["safety_hold"] or current.live_prescription["rehab_only"]


def test_clearance_removes_obsolete_generic_floor_but_preserves_original_checkin(context):
    live_injury(context, "ankle")
    set_sessions(context, [session("sparring", "Hard sparring", [
        dict(block_id="contact", block_type="sparring", mechanical_load_regions=["ankle"], contact_level="full")])])
    row = context[0].get_today_checkin(context[1], context[2], DAY)
    context[0].upsert_today_checkin(context[1], {**row, "recommendation_state": "pull_back",
        "recommendation_reason": "Old generic ankle restriction", "recommendation_triggers": ["active_injury_restriction"]})
    before = deepcopy(context[0].get_today_checkin(context[1], context[2], DAY))
    report(context, CONTACT)
    current = view(context)
    assert current.today.decision_tier == "green"
    assert not current.live_prescription["safety_hold"]
    assert context[0].get_today_checkin(context[1], context[2], DAY) == before


def test_clearance_does_not_remove_current_readiness_pullback(context):
    live_injury(context, "ankle")
    set_sessions(context, [live_camp("ankle")])
    report(context, CONTACT)
    row = context[0].get_today_checkin(context[1], context[2], DAY)
    context[0].upsert_today_checkin(context[1], {**row, "pain": "high"})
    current = view(context)
    assert current.today.decision_tier in {"stop", "pull_back"}
    assert current.live_prescription["safety_hold"] or current.live_prescription["rehab_only"]


@pytest.mark.parametrize("reverse", [False, True])
def test_two_live_clearances_keep_the_effective_no_contact_ceiling(context, reverse):
    live_injury(context, "ankle")
    set_sessions(context, [live_camp("ankle")])
    report(context, CONTACT)
    chest = context[0].create_injury_flag(context[1], dict(body_area="Chest", description="Chest strain",
        body_region="chest", severity="moderate", status="open"))
    report(context, TRAIN, injury=chest)
    if reverse:
        context[0].injury_flags[context[1]].reverse()
    current = view(context)
    assert current.effective_clinician_clearance["level"] == "train_no_contact"
    live = current.live_prescription
    assert not live["safety_hold"] and not live["rehab_only"]
    assert all(b.get("contact_level") != "full" for b in live["session"]["blocks"])
    assert any(b.get("block_id") == "skill" for b in live["session"]["blocks"])
    execute(context, live)
