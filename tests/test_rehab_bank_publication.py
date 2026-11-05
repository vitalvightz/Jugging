"""Bank classification -> shared policy -> real Today routes and completion."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

import pytest

from fightcamp.exercise_identity import normalize_exercise_key
from fightcamp.rehab_clinical import load_clinical_policies, load_pathway_catalog, validate_clinical_bank
from fightcamp.rehab_protocols import get_rehab_bank
from fightcamp.rehab_publication import bank_prescription, expand_bank_prescriptions
from tests.test_rehab_consumer_journey import PILOTS
from tests.test_rehab_experience import consumer, view, decision, HEADERS

GROIN = ("groin_strain", "Left groin", "strain", "", "mild")
OPTIONS = [
    (PILOTS[0], "achilles_unspecified_isometric_toe_presses_against_wall", ["wall"], "loading"),
    (PILOTS[1], "elbow_unspecified_pronation_supination_with_dowel", ["dowel"], "gentle_recovery"),
    (PILOTS[2], "ankle_unspecified_ankle_cars_controlled_articular_rotations", ["chair"], "gentle_recovery"),
    (PILOTS[3], "shoulder_impingement_scapular_wall_slides_with_chin_tuck", ["wall", "cloth"], "gentle_recovery"),
    (GROIN, "groin_unspecified_isometric_adductor_squeeze_ball_between_knees", ["soft_ball"], "gentle_recovery"),
]


# Source-dosed RESTORE expansion: bank options joined per live profile.
EXPANSION_COUNTS = {
    "wrist_sprain": 1, "wrist_stiffness": 1, "quads_contusion": 2, "biceps_contusion": 1, "triceps_contusion": 1,
    "forearm_contusion": 2, "shoulder_pain": 2, "shoulder_tightness": 2, "shoulder_soreness": 2, "elbow_pain": 1,
    "elbow_stiffness": 1, "wrist_pain": 2, "knee_pain": 3, "hip_pain": 2, "neck_tightness": 2, "neck_soreness": 2,
}


def candidate(identity):
    return next((deepcopy(g), deepcopy(d)) for g in get_rehab_bank() for d in g["drills"] if d["id"] == identity)


def test_current_complete_bank_options_expand_five_pilots_and_open_no_stages():
    catalog = load_pathway_catalog()
    before = load_clinical_policies(bank=[])
    after = load_clinical_policies()
    assert not catalog.bank_publication_profiles  # no per-profile rollout gate
    for old, new in zip(before, after):
        assert new.live_stages == old.live_stages
        assert new.transitions == old.transitions
        assert new.prescriptions[:len(old.prescriptions)] == old.prescriptions
        if old.policy_id not in {option[0][0] for option in OPTIONS} | set(EXPANSION_COUNTS):
            assert old == new
    added = {o.policy_id: len(n.prescriptions) - len(o.prescriptions) for o, n in zip(before, after)}
    assert sum(added.values()) == 5 + sum(EXPANSION_COUNTS.values())
    assert {k: v for k, v in added.items() if k in EXPANSION_COUNTS} == EXPANSION_COUNTS
    expected_counts = [(1, 1, 2), (1, 2, 1), (1, 3, 1), (1, 2, 1), (1, 2, 0)]
    for option, counts in zip(OPTIONS, expected_counts):
        policy = next(p for p in after if p.policy_id == option[0][0])
        assert tuple(sum(p.stage == s for p in policy.prescriptions) for s in ("calm", "restore", "load")) == counts
    assert validate_clinical_bank(after, get_rehab_bank()) == []


def test_complete_classification_needs_no_identity_allowlist_or_approval_record():
    group, drill = candidate(OPTIONS[0][1])
    drill["id"] = "achilles_unspecified_synthetic_same_mechanics"
    group["drills"] = [drill]
    policy = next(p for p in load_clinical_policies(bank=[]) if p.policy_id == "achilles_tendonitis")
    expanded = expand_bank_prescriptions(policy, [group])
    assert expanded.prescriptions[-1].drill_id == drill["id"]
    assert expanded.prescriptions[-1].bank_hash != policy.prescriptions[-1].bank_hash
    assert expanded.prescriptions[-1].clinical_criterion is None
    assert expanded.prescriptions[-1].dose.model_dump(exclude_none=True) == {"reps": 10, "duration_seconds": 10}
    # No unrelated injury receives a generic/unspecified record by default.
    assert expand_bank_prescriptions(policy.model_copy(update={"injury_type": "strain"}), [group]).prescriptions == policy.prescriptions
    # Ambiguous identity cannot win by file order.
    assert expand_bank_prescriptions(policy, [group, deepcopy(group)]) == policy


@pytest.mark.parametrize("field,value", [("load", "unknown"), ("equipment", None), ("dose", None),
    ("allowed_severities", None), ("stop_when", []), ("target_tissues", []),
    ("compatible_injury_types", None), ("notes", ""), ("prescription", {})])
def test_incomplete_classification_is_not_published(field, value):
    group, drill = candidate(OPTIONS[0][1])
    drill[field] = value
    prescription, reasons = bank_prescription(group, drill)
    assert prescription is None and reasons


def test_missing_loading_limits_and_duplicate_dose_owner_fail_closed():
    group, drill = candidate(OPTIONS[0][1])
    del drill["prescription"]["resistance"]
    assert bank_prescription(group, drill)[0] is None
    group, drill = candidate(OPTIONS[0][1])
    drill["prescription"]["dose"] = {"reps": 999}
    assert bank_prescription(group, drill)[1] == ["prescription_overrides_bank_classification"]


def test_surface_care_and_unknown_tissues_cannot_be_published_as_msk_content():
    group, drill = candidate(OPTIONS[0][1])
    assert "not_a_musculoskeletal_bank_group" in bank_prescription({**group, "type": "cut"}, drill)[1]
    assert bank_prescription(group, {**drill, "target_tissues": ["unknown"]})[0] is None


def configured_consumer(monkeypatch, option, *, progress_to_load=True):
    pilot, identity, equipment, permission = option
    journey, client = consumer(monkeypatch, pilot)
    journey.store.intakes[journey.athlete][0]["equipment_access"] = equipment
    journey.permission(permission)
    # #3003 makes permission a ceiling, independent of progression. Exercise
    # the existing real completion/response journey before selecting LOAD.
    if progress_to_load and permission == "loading":
        journey.reach_load()
    body = view(client)
    return journey, client, body


@pytest.mark.parametrize("option", OPTIONS, ids=lambda o: o[0][0])
def test_each_new_bank_option_selected_displayed_and_completed_through_today(monkeypatch, option):
    from api.services import exercise_media
    journey, client, body = configured_consumer(monkeypatch, option)
    live = body["live_prescription"]
    assert live and not live["safety_hold"], decision(body)
    block = live["session"]["blocks"][0]
    assert block["rehab_drill_id"] == option[1], decision(body)
    assert len(live["session"]["blocks"]) == 1  # no partial bundle or stacked independent routines
    _, bank = candidate(option[1])
    assert block["exercise_key"] == normalize_exercise_key(option[1])
    assert block["dose"] == bank["dose"]
    assert block["instructions"] == bank["notes"]
    assert block["drill_snapshot"]["equipment"] == bank["equipment"]
    assert block["stop_rules"] == bank["stop_when"]
    assert not any("clinical_review" in k for k in block)
    if option[0] == GROIN:
        assert decision(body)["progression"]["next_transition"]["target_stage_live"] is False
    # Exact media identity plugs into #2979; URLs are never inferred from names.
    monkeypatch.setattr(journey.store, "list_exercise_media", lambda: [dict(
        exercise_key=block["exercise_key"], video_id="AAAAAAAAAAA", made_for_kids=False, source="curated")])
    exercise_media.reset_media_index_cache()
    assert view(client)["exercise_media"][f"exercise:{block['exercise_key']}"]["video_id"] == "AAAAAAAAAAA"
    journey.now += timedelta(minutes=1)
    prior_exposure_ids = set(journey.store.rehab_exposures)
    for status in ("started", "done"):
        response = client.post("/api/today/session-completion", headers=HEADERS, json=dict(
            plan_id=journey.plan, session_id=live["session"]["session_id"],
            prescription_revision=live["revision"], rehab_tracking="injury_checkin", status=status,
            **({"rehab_performance": "done_as_shown"} if status == "done" else {})))
        assert response.status_code == 201, response.text
    # LOAD setup may already have real RESTORE evidence; attribute exactly the
    # one new event to the exercise just completed, rather than the setup work.
    new_exposure_ids = set(journey.store.rehab_exposures) - prior_exposure_ids
    assert len(new_exposure_ids) == 1
    event = journey.store.rehab_exposures[new_exposure_ids.pop()]["event_json"]
    assert event["drill_id"] == option[1]
    assert event["response"]["during_response"] == "not_reported"
    if bank["dose"]:
        assert event["dose_completed"]["completion_state"] == "quantified"
        for field, amount in bank["dose"].items():
            assert event["dose_completed"][field] == amount
    else:
        assert event["dose_completed"]["completion_state"] == "performed_amount_unknown"
    assert decision(view(client))["schedule"]["state"] == "already_completed"


def test_new_load_uses_existing_permission_pending_response_and_setback_holds(monkeypatch):
    journey, client, body = configured_consumer(monkeypatch, OPTIONS[0], progress_to_load=False)
    # #3003: permission alone cannot promote an episode to LOAD.
    assert decision(body)["stage"] == "restore"
    assert not journey.store.rehab_exposures
    assert "no_reviewed_stage_exposure" in decision(body)["progression"]["next_transition"]["reason_codes"]
    journey.reach_load()
    assert decision(view(client))["stage"] == "load"
    journey.permission("gentle_recovery")
    assert decision(view(client))["stage"] == "restore"
    journey.complete()
    journey.advance()
    journey.permission("loading")
    current = decision(view(client))
    assert current["stage"] == "restore"
    assert "next_day_response_not_yet_known" in current["progression"]["next_transition"]["reason_codes"]
    journey.report("ongoing")
    assert decision(view(client))["stage"] == "load"
    journey.report("worse")
    current = decision(view(client))
    assert current["stage"] != "load"


def test_timed_load_freeze_accepts_only_exact_prescribed_duration(monkeypatch):
    from api.services.clinical_review_freeze import frozen_review_hold
    journey, _, body = configured_consumer(monkeypatch, OPTIONS[0])
    live = body["live_prescription"]
    assert not frozen_review_hold(journey.store, journey.athlete, live, work_state="unstarted", as_of=journey.now)
    for value in ({"value": 100, "unit": "seconds"}, {"value": 10, "unit": "minutes"}, None):
        changed = deepcopy(live)
        changed["session"]["blocks"][0]["duration"] = value
        assert frozen_review_hold(journey.store, journey.athlete, changed, work_state="unstarted", as_of=journey.now)
    journey.permission("not_cleared")
    assert frozen_review_hold(journey.store, journey.athlete, live, work_state="unstarted", as_of=journey.now)
    assert not frozen_review_hold(journey.store, journey.athlete, live, work_state="started", as_of=journey.now)


def test_original_ankle_bundle_stays_atomic_when_all_equipment_is_available(monkeypatch):
    journey, client = consumer(monkeypatch, PILOTS[2])
    journey.permission("gentle_recovery")
    blocks = view(client)["live_prescription"]["session"]["blocks"]
    assert {b["rehab_drill_id"] for b in blocks} == {"ankle_sprain_supported_balance", "ankle_sprain_heel_lowering"}


if __name__ == "__main__":
    import sys
    fixtures = {}
    for option in OPTIONS:
        with pytest.MonkeyPatch.context() as patch:
            journey, client, body = configured_consumer(patch, option)
            fixtures[option[0][0]] = dict(view=body, profile=client.get("/api/me", headers=HEADERS).json())
    Path(sys.argv[1]).write_text(json.dumps(fixtures, indent=2) + "\n", encoding="utf-8")
