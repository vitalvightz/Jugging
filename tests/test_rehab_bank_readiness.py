"""Shared publication and real Today selection, reusing the completed audits."""
from copy import deepcopy

import pytest

from api.contracts.injury_policy import resolve_injury_policy
from fightcamp import rehab_protocols
from fightcamp.rehab_clinical import ClinicalDose, compose_policy, load_clinical_policies, load_pathway_catalog, policy_review_hash
from tests.test_rehab_bank_publication import OPTIONS, candidate
from tests.test_rehab_experience import consumer, decision, view

FAMILIES = [
    ("chest_strain", "Chest", "strain", "", "mild"),
    ("ankle_sprain", "Left ankle", "sprain", "ankle_scope:uncomplicated_lateral", "mild"),
    ("achilles_tendonitis", "Left Achilles", "tendonitis", "achilles_site:midportion", "mild"),
    ("hip_impingement", "Left hip", "impingement", "", "mild"),
    ("elbow_hyperextension", "Left elbow", "hyperextension", "", "mild"),
    ("elbow_contusion", "Left elbow", "contusion", "", "mild"),
    ("neck_stiffness", "Neck", "stiffness", "", "mild"),
]


@pytest.mark.parametrize("pilot", FAMILIES, ids=lambda p: p[0])
def test_all_families_share_complete_compatible_publication_without_a_rollout_list(pilot):
    catalog = load_pathway_catalog().model_copy(update={"bank_publication_profiles": []})
    profile = next(p for p in catalog.profiles if p["policy_id"] == pilot[0])
    before = compose_policy(catalog, profile, bank=[])
    group, drill = candidate(OPTIONS[1][1])
    drill.update(id="synthetic_reviewed_family_option", target_regions=[before.region],
        compatible_injury_types=[before.injury_type],
        rehab_stage="restore" if "restore" in before.live_stages else "calm")
    group.update(location=before.region, type=before.injury_type, drills=[drill])
    after = compose_policy(catalog, profile, bank=[group])
    assert len(after.prescriptions) == len(before.prescriptions) + 1
    assert after.prescriptions[-1].drill_id == drill["id"]
    assert after.live_stages == before.live_stages and after.transitions == before.transitions
    # Neither family membership nor a stage label supplies missing content.
    for changed in ({"compatible_injury_types": []}, {"dose": None}, {"rehab_stage": "dynamic"}):
        blocked = deepcopy(group)
        blocked["drills"][0].update(changed)
        assert compose_policy(catalog, profile, bank=[blocked]).prescriptions == before.prescriptions


@pytest.mark.parametrize("pilot", FAMILIES, ids=lambda p: p[0])
@pytest.mark.parametrize("phase", ["GPP", "SPP", "TAPER"])
def test_today_preserves_exact_published_work_in_every_camp_phase(monkeypatch, pilot, phase):
    options = {"initial_status": "ongoing", "edit_subtype": False} if pilot[0] == "elbow_hyperextension" else {}
    journey, client = consumer(monkeypatch, pilot, **options)
    journey.store.plans[journey.plan]["structured_plan"]["weeks"][0]["phase_label"] = phase
    body = view(client)
    current = decision(body)
    if pilot[0] == "elbow_hyperextension":
        # This family's profiles only publish CALM. A follow-up can resolve
        # RESTORE, but no unapproved RESTORE exercise may be substituted.
        # #3003 distinguishes unavailable content from missing athlete data.
        assert current["outcome"] == "unsupported_prescription"
        assert current["reason_codes"] == ["stage_not_activated"]
        assert current["prescription"] is None
        return
    assert current["outcome"] == "prescribed_rehab", current
    assert current["stage"] in journey.policy.live_stages
    assert current["stage"] not in {"dynamic", "return"}
    prescription = current["prescription"]
    approved = next(p for p in journey.policy.prescriptions if p.drill_id == prescription["drill_id"])
    assert prescription["instructions"] == approved.instructions
    assert prescription["dose"] == (approved.dose.model_dump(exclude_none=True) if approved.dose else {})
    blocks = body["live_prescription"]["session"]["blocks"]
    for block in blocks:
        if block.get("block_type") != "rehab":
            assert current["schedule"]["state"] != "due"
            continue
        approved = next(p for p in journey.policy.prescriptions if p.drill_id == block["rehab_drill_id"])
        # An already accepted CALM snapshot remains valid after a follow-up;
        # refreshing Today must not replace it with a harder prescription.
        assert ["calm", "restore", "load", "dynamic", "return"].index(approved.stage) <= [
            "calm", "restore", "load", "dynamic", "return"].index(current["stage"])
        assert block["instructions"] == approved.instructions
        assert block["dose"] == (approved.dose.model_dump(exclude_none=True) if approved.dose else {})
        assert block["stop_rules"] == approved.stop_when


def test_camp_dose_cannot_replace_an_injury_prescription():
    policy = next(p for p in load_clinical_policies() if p.policy_id == "chest_strain")
    prescriptions = list(policy.prescriptions)
    prescriptions[1] = prescriptions[1].model_copy(update={"camp_doses": {"TAPER": ClinicalDose(reps=999)}})
    policy = policy.model_copy(update={"prescriptions": prescriptions})
    policy = policy.model_copy(update={"content_hash": policy_review_hash(policy)})
    injury = dict(id="injury", episode_id="episode", athlete_id="athlete", body_region="chest",
        canonical_location="chest", injury_type="strain", severity="mild", rehab_stage="restore",
        latest_reported_status="improving", latest_reported_at="2026-10-05T12:00:00Z")
    for phase in ("GPP", "SPP", "TAPER"):
        resolved = resolve_injury_policy(injury, policies=[policy], bank=rehab_protocols.get_rehab_bank(), phase=phase)
        assert resolved["outcome"] == "prescribed_rehab"
        assert resolved["prescription"]["dose"] == {}  # approved self-paced work, not invented reps


@pytest.mark.parametrize("phase", ["GPP", "SPP", "TAPER"])
def test_unpublished_msk_profile_never_uses_legacy_phase_content(phase):
    assert rehab_protocols.rehab_drill_options_for_phase("hyperextension", "ankle", phase) == []
    assert rehab_protocols.rehab_drill_options_for_phase("hyperextension", "ankle", phase,
        injury={"id": "test", "episode_id": "test", "body_region": "ankle", "severity": "mild"},
        rehab_stage="restore") == []


def test_label_repair_changes_no_mechanics_or_prescription():
    identity = "wrist_sprain_wrist_stability_drill_with_dynamometer"
    _, drill = candidate(identity)
    assert drill["name"] == "Supported dynamometer grip loading"
    assert drill["dose"] is None and drill["load"] == "unknown"
    assert "not a test of ligament healing or clearance" in drill["notes"]
    assert all(p.drill_id != identity for policy in load_clinical_policies() for p in policy.prescriptions)


@pytest.mark.parametrize("option", OPTIONS, ids=lambda o: o[0][0])
def test_generation_keeps_the_same_owned_prescription_and_props_as_today(monkeypatch, option):
    from api.services.rehab_stage_snapshot import resolve_open_injury_rehab_context
    from tests.test_rehab_bank_publication import configured_consumer
    from fightcamp.stage2_payload import _build_rehab_slots
    journey, client, body = configured_consumer(monkeypatch, option)
    contexts = resolve_open_injury_rehab_context(journey.store, journey.athlete)
    context = next(c for c in contexts.values() if c["injury_id"] == journey.injury["id"])
    assert context["available_equipment"] == option[2]
    assert context["policy_injury"]["id"] == journey.injury["id"]
    episode = {**context, "canonical_location": journey.policy.region,
        "injury_type": journey.policy.injury_type, "severity": option[0][4]}
    text, _ = rehab_protocols.generate_rehab_protocols(
        injury_string=option[0][1], exercise_data=[], current_phase="GPP", parsed_entries=[episode])
    slots = _build_rehab_slots(text, "GPP")
    assert slots, text
    assert slots[0]["selected"]["rehab_drill_id"] == decision(body)["prescription"]["drill_id"]


@pytest.mark.parametrize("pilot", FAMILIES, ids=lambda p: p[0])
def test_generation_carries_owned_context_for_every_family(monkeypatch, pilot):
    from api.services.rehab_stage_snapshot import resolve_open_injury_rehab_context
    journey, client = consumer(monkeypatch, pilot)
    contexts = resolve_open_injury_rehab_context(journey.store, journey.athlete)
    context = next(c for c in contexts.values() if c["injury_id"] == journey.injury["id"])
    assert context["policy_injury"]["id"] == journey.injury["id"]
    assert context["policy_injury"]["episode_id"] == journey.injury["episode_id"]
    assert context["policy_injury"]["injury_type"] == journey.policy.injury_type
    assert context["available_equipment"] == ["stable_support", "table", "chair", "wall"]
