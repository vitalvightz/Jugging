"""Published medical guidance stays informative, never an executable drill."""
import pytest

from api.structured_plan_safety import audit_structured_plan
from fightcamp.rehab_clinical import load_clinical_policies
from fightcamp.rehab_protocols import generate_rehab_protocols, rehab_drill_by_id
from fightcamp.stage2_payload import _build_rehab_slots


def _published_calm(policy_id):
    policy = next(p for p in load_clinical_policies() if p.policy_id == policy_id)
    assert policy.activation == "live"
    prescription = next(rx for rx in policy.prescriptions if rx.stage == "calm")
    drill = rehab_drill_by_id(prescription.drill_id)
    assert drill is not None
    return policy, prescription, drill


@pytest.mark.parametrize("policy_id", ["knee_instability", "shoulder_impingement"])
def test_published_protection_guidance_is_summary_only(policy_id):
    policy, prescription, _ = _published_calm(policy_id)
    text, seen = generate_rehab_protocols(
        injury_string="",
        parsed_entries=[{"canonical_location": policy.region, "injury_type": policy.injury_type,
                         "rehab_stage": "calm", "severity": "low"}],
        exercise_data=[], current_phase="GPP",
    )
    assert prescription.instructions in text
    assert all(rule in text for rule in prescription.stop_when)
    assert "  • " not in text
    assert not _build_rehab_slots(text, "GPP")
    assert not seen


@pytest.mark.parametrize("policy_id", ["knee_instability", "shoulder_impingement"])
def test_protection_guidance_cannot_become_a_structured_exercise(policy_id):
    _, _, drill = _published_calm(policy_id)
    plan = {"weeks": [{"days": [{"sessions": [{"title": "Rehab", "session_type": "rehab",
        "blocks": [{"display_name": drill["name"], "block_type": "rehab", "sets": 2, "reps": 10}]}]}]}]}
    assert any("medical guidance presented as exercise" in finding for finding in audit_structured_plan(plan))


def test_published_exercise_still_reaches_the_rehab_card():
    policy = next(p for p in load_clinical_policies() if p.policy_id == "ankle_sprain")
    assert policy.activation == "live"
    text, _ = generate_rehab_protocols(
        injury_string="",
        parsed_entries=[{"canonical_location": "ankle", "injury_type": "sprain", "severity": "moderate",
                         "rehab_stage": "restore", "laterality": "left", "available_equipment": ["stable_support"]}],
        exercise_data=[], current_phase="GPP",
    )
    slots = _build_rehab_slots(text, "GPP")
    assert slots
    assert {slot["selected"]["rehab_drill_id"] for slot in slots} <= {
        rx.drill_id for rx in policy.prescriptions if rx.stage == "restore"
    }
