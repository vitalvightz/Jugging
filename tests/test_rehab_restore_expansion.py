"""Source-dosed RESTORE expansion: real Today selection, completion and safety boundaries."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from api.contracts.injury_policy import resolve_injury_policy
from fightcamp.exercise_identity import normalize_exercise_key
from fightcamp.rehab_clinical import load_clinical_policies
from fightcamp.rehab_protocols import get_rehab_bank
from tests.test_rehab_experience import HEADERS, consumer, decision, view

ROOT = Path(__file__).resolve().parents[1]
HISTORY = json.loads((ROOT / "tests/fixtures/rehab_restore_expansion_history.json").read_text(encoding="utf-8"))
# Published identities: in-place reviews whose ID still names the movement, plus new IDs.
NEW_IDS = set(HISTORY["drills"]) | set(HISTORY["added"]["drills"])
OPENED = set(HISTORY["profiles"])

KNEE = ("knee_pain", "Left knee", "pain", "", "mild")
QUADS = ("quads_contusion", "Left quads", "contusion", "", "mild")
NECK_TIGHT = ("neck_tightness", "Left neck", "tightness", "", "mild")
NECK_SORE = ("neck_soreness", "Left neck", "soreness", "", "moderate")
SHOULDER_PAIN = ("shoulder_pain", "Left shoulder", "pain", "", "mild")
SHOULDER_TIGHT = ("shoulder_tightness", "Left shoulder", "tightness", "", "mild")
SHOULDER_SORE = ("shoulder_soreness", "Left shoulder", "soreness", "", "moderate")
ELBOW_PAIN = ("elbow_pain", "Left elbow", "pain", "", "mild")
ELBOW_STIFF = ("elbow_stiffness", "Left elbow", "stiffness", "", "mild")
BICEPS = ("biceps_contusion", "Left biceps", "contusion", "", "mild")
TRICEPS = ("triceps_contusion", "Left triceps", "contusion", "", "mild")
FOREARM = ("forearm_contusion", "Left forearm", "contusion", "", "mild")
WRIST_PAIN = ("wrist_pain", "Left wrist", "pain", "", "mild")
WRIST_STIFF = ("wrist_stiffness", "Left wrist", "stiffness", "", "mild")
WRIST_SPRAIN = ("wrist_sprain", "Left wrist", "sprain", "", "mild")
HIP = ("hip_pain", "Left hip", "pain", "", "mild")

# Each new identity with the reported props under which the real selector picks it.
OPTIONS = [
    (KNEE, "knee_pain_heel_slide_knee_bend", []),
    (KNEE, "knee_pain_seated_knee_straightening_hold", ["chair"]),
    (KNEE, "knee_pain_heel_lift_over_rolled_towel", ["chair", "towel"]),
    (QUADS, "quads_pain_isometric_quad_flex_straight_leg", []),
    (QUADS, "quads_unspecified_sliding_leg_extensions_towel_under_foot", ["towel"]),
    (NECK_TIGHT, "neck_soreness_seated_forward_neck_bend", []),
    (NECK_SORE, "neck_tightness_seated_neck_side_tilt", ["chair"]),
    (SHOULDER_PAIN, "shoulder_unspecified_lying_assisted_arm_raise", []),
    (SHOULDER_TIGHT, "shoulder_swelling_pendulum_circles", ["stable_support"]),
    (SHOULDER_SORE, "shoulder_unspecified_lying_assisted_arm_raise", ["chair"]),
    (ELBOW_PAIN, "elbow_stiffness_elbow_bend_and_straighten", []),
    (ELBOW_STIFF, "elbow_stiffness_elbow_bend_and_straighten", ["table"]),
    (BICEPS, "biceps_contusion_comfortable_elbow_bend", []),
    (TRICEPS, "triceps_contusion_comfortable_elbow_bend", []),
    (FOREARM, "forearm_contusion_elbow_at_side_forearm_turn", []),
    (FOREARM, "forearm_contusion_supported_wrist_bend", ["table"]),
    (WRIST_PAIN, "wrist_pain_forearm_supination_pronation_paused", []),
    (WRIST_PAIN, "wrist_pain_side_to_side_wrist_bend", ["table"]),
    (WRIST_STIFF, "wrist_unspecified_finger_extension_with_wrist_flexion", []),
    (WRIST_SPRAIN, "wrist_unspecified_finger_extension_with_wrist_flexion", ["chair"]),
    (HIP, "hip_pain_double_leg_bridge_hold", []),
    (HIP, "hip_pain_supported_standing_hip_abduction", ["stable_support"]),
]


def bank_drill(identity):
    return next(deepcopy(d) for g in get_rehab_bank() for d in g["drills"] if d["id"] == identity)


def journey_for(monkeypatch, pilot, equipment):
    journey, client = consumer(monkeypatch, pilot)
    journey.store.intakes[journey.athlete][0]["equipment_access"] = equipment
    journey.permission("gentle_recovery")
    return journey, client


def probe(policy_id, *, stage="restore", severity="mild", **extra):
    policy = next(p for p in load_clinical_policies() if p.policy_id == policy_id)
    return dict(id="probe", episode_id="episode", athlete_id="athlete", side="left",
        body_area=f"Left {policy.region}", canonical_location=policy.region, body_region=policy.region,
        injury_type=policy.injury_type, description=policy.injury_type, severity=severity,
        status="monitoring", latest_reported_status="improving", rehab_stage=stage, **extra)


def test_every_new_identity_is_exercised_and_opened_profiles_match_history():
    assert {identity for _, identity, _ in OPTIONS} == NEW_IDS
    assert {pilot[0] for pilot, _, _ in OPTIONS} - {"wrist_stiffness", "wrist_sprain"} == OPENED


@pytest.mark.parametrize("option", OPTIONS, ids=lambda o: f"{o[0][0]}-{o[1]}")
def test_new_option_selected_displayed_completed_and_followed_up_through_today(monkeypatch, option):
    pilot, identity, equipment = option
    journey, client = journey_for(monkeypatch, pilot, equipment)
    body = view(client)
    current = decision(body)
    assert current["stage"] == "restore" and current["outcome"] == "prescribed_rehab", current
    assert current["progression"]["next_transition"]["target_stage_live"] is False
    live = body["live_prescription"]
    assert live and not live["safety_hold"]
    block = live["session"]["blocks"][0]
    assert block["rehab_drill_id"] == identity and len(live["session"]["blocks"]) == 1
    drill = bank_drill(identity)
    assert block["exercise_key"] == normalize_exercise_key(identity)
    assert block["dose"] == drill["dose"] and drill["dose"]
    assert block["instructions"] == drill["notes"]
    assert block["drill_snapshot"]["equipment"] == drill["equipment"]
    assert set(drill["equipment"]) <= set(equipment)
    assert block["stop_rules"] == drill["stop_when"]
    for status in ("started", "done"):
        response = client.post("/api/today/session-completion", headers=HEADERS, json=dict(
            plan_id=journey.plan, session_id=live["session"]["session_id"],
            prescription_revision=live["revision"], rehab_tracking="injury_checkin", status=status,
            **({"rehab_performance": "done_as_shown"} if status == "done" else {})))
        assert response.status_code == 201, response.text
    event = next(iter(journey.store.rehab_exposures.values()))["event_json"]
    assert event["drill_id"] == identity and event["provenance"]["policy_id"] == pilot[0]
    assert event["dose_completed"]["completion_state"] == "quantified"
    for field, amount in drill["dose"].items():
        assert event["dose_completed"][field] == amount
    assert decision(view(client))["schedule"]["state"] == "already_completed"
    journey.advance()
    assert journey.view().delayed_rehab_prompts  # next-day response capture still works


@pytest.mark.parametrize("pilot", [KNEE, SHOULDER_PAIN, FOREARM, HIP], ids=lambda p: p[0])
def test_worse_report_returns_to_calm_and_new_restore_work_is_not_offered(monkeypatch, pilot):
    journey, client = journey_for(monkeypatch, pilot, ["chair", "towel", "table", "stable_support"])
    assert decision(view(client))["stage"] == "restore"
    journey.report("worse")
    current = decision(view(client))
    assert current["stage"] == "calm"
    assert (current["prescription"] or {}).get("drill_id") not in NEW_IDS


@pytest.mark.parametrize("phase", ["GPP", "SPP", "TAPER"])
def test_camp_phase_cannot_change_stage_selection_or_approved_dose(monkeypatch, phase):
    journey, client = journey_for(monkeypatch, KNEE, ["chair"])
    journey.store.plans[journey.plan]["structured_plan"]["weeks"][0]["phase_label"] = phase
    body = view(client)
    block = body["live_prescription"]["session"]["blocks"][0]
    assert decision(body)["stage"] == "restore"
    assert block["rehab_drill_id"] == "knee_pain_seated_knee_straightening_hold"
    assert block["dose"] == {"reps": 10}
    for camp in ("GPP", "SPP", "TAPER"):
        assert resolve_injury_policy(probe("knee_pain"), policies=load_clinical_policies(), bank=get_rehab_bank(),
            phase=camp, equipment=["chair"])["prescription"]["dose"] == {"reps": 10}


@pytest.mark.parametrize("policy_id", sorted(OPENED))
def test_opened_profiles_keep_higher_stages_closed_and_severity_gates(policy_id):
    policies, bank = load_clinical_policies(), get_rehab_bank()
    policy = next(p for p in policies if p.policy_id == policy_id)
    assert policy.live_stages == ["calm", "restore"]
    assert not any(t.promotable for t in policy.transitions)
    assert all(p.stage in {"calm", "restore"} and not p.required_rehabilitation_level for p in policy.prescriptions)
    for stage in ("load", "dynamic", "return"):
        row = probe(policy_id, stage=stage, clinician_clearance=dict(scopes=["rehab", "training", "contact"]))
        assert (resolve_injury_policy(row, policies=policies, bank=bank).get("prescription") or {}).get("drill_id") not in NEW_IDS
    # Existing high-severity/medical gates still decide before any new content.
    high = resolve_injury_policy(probe(policy_id, severity="severe"), policies=policies, bank=bank)
    assert (high.get("prescription") or {}).get("drill_id") not in NEW_IDS


def test_unresolved_adverse_response_rejects_the_new_option():
    from fightcamp.rehab_selector import filter_rehab_candidates
    drill = {**bank_drill("hip_pain_double_leg_bridge_hold"), "injury_type": "pain"}
    injury = probe("hip_pain")
    exposure = dict(athlete_id="athlete", injury_id="probe", injury_episode_id="episode", drill_id=drill["id"],
        body_region="hip", side="left", response=dict(during_response="worse"))
    eligible, rejected = filter_rehab_candidates(injury={**injury, "body_region_aliases": ["hip"]}, rehab_stage="restore",
        candidates=[drill], exposures=[exposure], activated_stages=["calm", "restore"])
    assert not eligible and "REJECT_UNRESOLVED_NEGATIVE_EXPOSURE" in rejected[0].reason_codes


def test_reduced_training_days_keep_the_same_rehab():
    policies, bank = load_clinical_policies(), get_rehab_bank()
    normal = resolve_injury_policy(probe("hip_pain"), policies=policies, bank=bank)
    for verdict in ("modify", "pull_back"):
        reduced = resolve_injury_policy(probe("hip_pain"), policies=policies, bank=bank, readiness_decision=verdict)
        assert reduced["prescription"] == normal["prescription"]


def test_two_new_injuries_keep_their_own_region_restrictions_and_prescriptions():
    policies, bank = load_clinical_policies(), get_rehab_bank()
    knee = resolve_injury_policy(probe("knee_pain"), policies=policies, bank=bank, equipment=["chair", "towel"])
    wrist = resolve_injury_policy({**probe("wrist_pain"), "id": "probe-2", "episode_id": "episode-2"},
        policies=policies, bank=bank, equipment=["chair", "towel"])
    assert knee["prescription"]["drill_id"] == "knee_pain_heel_lift_over_rolled_towel"
    assert wrist["prescription"]["drill_id"] == "wrist_pain_forearm_supination_pronation_paused"
    assert knee["restrictions"] != wrist["restrictions"]
    assert "knee" in json.dumps(knee["restrictions"]) and "wrist" in json.dumps(wrist["restrictions"])


@pytest.mark.parametrize("option", OPTIONS, ids=lambda o: f"{o[0][0]}-{o[1]}")
def test_plan_generation_selects_the_same_new_option_as_today_in_every_camp_phase(monkeypatch, option):
    from api.services.rehab_stage_snapshot import resolve_open_injury_rehab_context
    from fightcamp import rehab_protocols
    from fightcamp.stage2_payload import _build_rehab_slots
    pilot, identity, equipment = option
    journey, client = journey_for(monkeypatch, pilot, equipment)
    assert decision(view(client))["prescription"]["drill_id"] == identity
    contexts = resolve_open_injury_rehab_context(journey.store, journey.athlete)
    context = next(c for c in contexts.values() if c["injury_id"] == journey.injury["id"])
    assert context["available_equipment"] == equipment
    episode = {**context, "canonical_location": journey.policy.region,
               "injury_type": journey.policy.injury_type, "severity": pilot[4]}
    for phase in ("GPP", "SPP", "TAPER"):
        text, _ = rehab_protocols.generate_rehab_protocols(
            injury_string=pilot[1], exercise_data=[], current_phase=phase, parsed_entries=[episode])
        slots = _build_rehab_slots(text, phase)
        assert slots and slots[0]["selected"]["rehab_drill_id"] == identity, (phase, text)


def test_generation_identity_uses_canonical_biceps_region_for_existing_content():
    from fightcamp.rehab_protocols import reviewed_rehab_drill_id_for_line
    line = "Supported elbow bend and straighten for biceps strain – Self-paced movement within comfort."
    assert reviewed_rehab_drill_id_for_line(line, "strain", "biceps") == "biceps_strain_reviewed_restore"
    assert reviewed_rehab_drill_id_for_line(line, "strain", "bicep") == "biceps_strain_reviewed_restore"


def test_movements_the_original_identity_does_not_describe_use_new_identities():
    """Original IDs keep their meaning for demos and recorded work; new movements get new IDs."""
    added = set(HISTORY["added"]["drills"])
    assert len(added) == 14 and not added & set(HISTORY["drills"])
    ledger = {r["drill_id"]: r for r in json.loads((ROOT / "data/rehab_metadata_review.json").read_text(encoding="utf-8"))}
    live = {p.drill_id for policy in load_clinical_policies() for p in policy.prescriptions}
    bank = {d["id"]: d for g in get_rehab_bank() for d in g["drills"]}
    for identity in added:
        assert ledger[identity]["review_state"] == "reviewed" and "source_history" not in ledger[identity]
        assert identity in live
    originals = ["knee_unspecified_sliding_hamstring_curls_sliders_towel", "triceps_contusion_wall_assisted_arm_circles",
                 "neck_soreness_gentle_neck_tilts_assisted", "shoulder_unspecified_supine_shoulder_figure_8s_with_dowel",
                 "forearm_soreness_passive_wrist_circles_tabletop_support", "hip_soreness_mini_band_standing_hip_abduction"]
    for identity in originals:
        assert identity not in live and ledger[identity]["review_state"] == "needs_review"
        assert "prescription" not in bank[identity] and bank[identity]["rehab_stage"] is None
