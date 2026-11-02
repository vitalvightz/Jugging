"""Scale and safety invariants of the pathway architecture.

Families route; profiles activate. Nothing here may let family membership,
shared safety rules, clearance, camp phase, time or readiness move a stage.
"""
from __future__ import annotations

import itertools
import json
import subprocess
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from api.contracts.injury_policy import reconcile_session_prescription, resolve_injury_policy
from fightcamp.rehab_clinical import (
    PATHWAYS_PATH, compose_policy, load_clinical_policies, load_pathway_catalog, policy_review_hash,
)
from fightcamp.rehab_protocols import get_rehab_bank
from tests.test_rehab_transition_engine import BANK, EPISODE, INJURY, clinical, event, injury, policy, progress

ROOT = Path(__file__).resolve().parents[1]
LEGACY = Path(__file__).parent / "fixtures" / "rehab_clinical_policies_v2_legacy.json"


# 1. The dead duplicate path is gone.
def test_no_runtime_reference_to_the_removed_load_eligibility_path():
    hits = subprocess.run(["git", "grep", "-l", "-E", "load_eligibility|LOAD_CRITERIA_REGISTRY|LoadCriteria", "--",
                           "*.py", "*.ts", "*.tsx", "*.sql"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert [h for h in hits if h != "tests/test_rehab_pathway_safety_invariants.py"] == []


# 2. Hashes, prescriptions and bundles match the pre-migration policy file.
def test_shipped_policies_match_the_pre_migration_file_exactly():
    from tests.rehab_pathway_migration import composed
    for before, after in zip(load_clinical_policies(LEGACY), composed()):
        assert policy_review_hash(after) == before.content_hash == after.content_hash
        assert after.prescriptions == before.prescriptions and after.stage_bundles == before.stage_bundles
        assert after.live_stages == before.live_stages


# 3. Exposure alone cannot activate LOAD; DYNAMIC and RETURN stay closed.
@pytest.mark.parametrize("policy_id", [p.policy_id for p in load_clinical_policies()])
def test_shipped_profiles_cannot_enter_a_higher_stage(policy_id):
    shipped = next(p for p in load_clinical_policies() if p.policy_id == policy_id)
    calm_only = (policy_id == "wrist_impingement" or shipped.injury_type == "hyperextension"
                 or policy_id in {f"{r}_contusion" for r in ["heel", "shin"]}
                     or policy_id in {'lower_back_stiffness', 'hand_pain', 'fingers_pain'})
    expected_stage = "calm" if calm_only else "restore"
    assert shipped.live_stages == (["calm","restore","load"] if policy_id in {"achilles_tendonitis", "elbow_tendonitis", "ankle_sprain", "shoulder_tendonitis"} else
        ["calm"] if expected_stage == "calm" else ["calm","restore"])
    assert not any(t.promotable for t in (shipped.transitions[1:] if policy_id in {"achilles_tendonitis", "elbow_tendonitis", "ankle_sprain", "shoulder_tendonitis"} else shipped.transitions))
    assert bool(shipped.transitions[0].promotable) == (policy_id in {"achilles_tendonitis", "elbow_tendonitis", "ankle_sprain", "shoulder_tendonitis"})
    region, kind = shipped.region, shipped.injury_type
    row = injury(body_region=region, canonical_location=region, injury_type=kind, description=f"{region} {kind}")
    if expected_stage == "calm":
        row["rehab_stage"] = "calm"
    drill_id = next(p.drill_id for p in shipped.prescriptions if p.stage == expected_stage)
    drill = next(d for g in get_rehab_bank() for d in g["drills"] if d["id"] == drill_id)
    ideal = [event(n, drill=drill, policy_id=policy_id, completion="quantified") for n in range(1, 6)]
    for raw in ideal:
        raw["event_json"]["body_region"] = region
        raw["event_json"]["demand"]["target_regions"] = [region]
    decision = resolve_injury_policy(row, policies=(shipped,), bank=get_rehab_bank(), exposures=ideal)
    assert decision["stage"] == expected_stage
    if expected_stage == "calm":
        assert "next_transition" not in decision["progression"]
    elif policy_id == "elbow_tendonitis":
        next_transition = decision["progression"]["next_transition"]
        assert next_transition["status"] == "blocked" and next_transition["target_stage_live"]
        assert next_transition["reason_codes"] == ["elbow_lateral_applicability_not_confirmed"]
        # Ideal completed work cannot invent location, even with permission.
        permission = dict(episode_id=row["episode_id"], rehabilitation_permission=dict(schema_version=1, level="loading"))
        missing_location = resolve_injury_policy({**row, "clinician_clearance": permission}, policies=(shipped,),
            bank=get_rehab_bank(), exposures=ideal, equipment=["table"])
        assert missing_location["stage"] == "restore"
        assert missing_location["progression"]["next_transition"]["reason_codes"] == ["elbow_lateral_applicability_not_confirmed"]
        # Location also cannot manufacture the separate rehabilitation permission.
        missing_permission = resolve_injury_policy({**row, "description": row["description"] + " [elbow_site:lateral]"},
            policies=(shipped,), bank=get_rehab_bank(), exposures=ideal, equipment=["table"])
        assert missing_permission["stage"] == "restore"
        assert missing_permission["progression"]["next_transition"]["reason_codes"] == ["rehabilitation_loading_not_reported"]
    elif policy_id == "shoulder_tendonitis":
        assert decision["progression"]["next_transition"]["reason_codes"] == ["shoulder_nontraumatic_cuff_not_reported"]
    elif policy_id == "ankle_sprain":
        assert decision["progression"]["next_transition"]["reason_codes"] == ["ankle_uncomplicated_lateral_not_reported"]
    elif policy_id == "achilles_tendonitis":
        assert decision["progression"]["next_transition"]["reason_codes"] == ["rehabilitation_loading_not_reported"]
    else:
        assert decision["progression"]["next_transition"]["reason_codes"] == ["no_clinical_criteria_declared"]


# 4. Shared safety/data rules alone never promote, in any family.
@pytest.mark.parametrize("family_id", [f.family_id for f in load_pathway_catalog().families])
def test_family_without_clinical_criteria_never_promotes(family_id):
    catalog = load_pathway_catalog()
    family = catalog.family(family_id)
    assert all(r.basis != "clinical" for t in family.transitions for r in t.requirements)
    profile = dict(policy_id="probe", version=1, pathway_family=family_id, region="ankle",
                   injury_type=family.injury_types[0], transition_overrides={})
    composed = compose_policy(catalog, profile)
    assert composed.transitions and not any(t.promotable for t in composed.transitions)
    with pytest.raises(ValidationError, match="cannot be live without an open transition"):
        compose_policy(catalog, {**profile, "live_stages": ["calm", "restore", "load"]})


# 5. A regional profile cannot remove product-safety requirements.
@pytest.mark.parametrize("requirement_id", [r.requirement_id for r in load_pathway_catalog().safety_baseline
                                            if r.basis == "product_safety"])
def test_profiles_cannot_remove_safety_requirements(requirement_id):
    with pytest.raises(ValueError, match="cannot remove a product safety requirement"):
        policy(remove=(requirement_id,))


def test_truncation_and_setback_checks_are_safety_requirements():
    bases = {r.requirement_id: r.basis for r in load_pathway_catalog().safety_baseline}
    assert bases["complete_episode_history"] == bases["no_unresolved_setback"] == "product_safety"


# 6. Unknown or missing functional inputs fail closed.
def test_uncaptured_or_undeclared_functional_inputs_fail_closed():
    walk = clinical("test_walk", "functional_checkpoint", checkpoint="pain_free_walking")
    result = progress([event()], current=policy(add=(clinical(), walk)))
    assert result["stage"] == "restore" and result["next_transition"]["missing_inputs"] == ["pain_free_walking"]
    with pytest.raises(ValueError, match="unknown functional checkpoint"):
        policy(add=(clinical("test_x", "functional_checkpoint", checkpoint="invented_check"),))


# 7. Higher-stage activation needs all four structural conditions.
def test_higher_stage_activation_requires_ladder_content_open_transition_and_clinical_criterion():
    policy()  # All four conditions met: valid.
    with pytest.raises(ValidationError, match="every lower stage"):
        policy(live=("calm", "load"))
    with pytest.raises(ValidationError, match="without a reviewed prescription"):
        policy(load_prescription=False)
    with pytest.raises(ValidationError, match="cannot be live without an open transition"):
        policy(closed="Clinician-led for this profile.")
    with pytest.raises(ValidationError, match="cannot be live without an open transition"):
        policy(add=())
    with pytest.raises(ValueError, match="must cite sources"):
        policy(add=({**clinical(), "sources": []},))


def test_legacy_policy_files_are_not_an_activation_route(tmp_path):
    raw = json.loads(LEGACY.read_text(encoding="utf-8"))
    raw["policies"][0]["live_stages"] = ["calm", "restore", "load"]
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_clinical_policies(path)


# 8. Clearance, camp phase, elapsed time and readiness never advance a stage.
@pytest.mark.parametrize("phase,readiness,clearance,created", itertools.product(
    ["", "GPP", "SPP", "TAPER"], [None, "modify", "pull_back", "train_as_planned"],
    [None, ["rehab"], ["rehab", "training", "contact"]], ["2026-08-30T00:00:00Z", "2024-01-01T00:00:00Z"]))
def test_non_injury_inputs_never_advance_stage(phase, readiness, clearance, created):
    row = injury(created_at=created, updated_at=created)
    if clearance:
        row["clinician_clearance"] = dict(episode_id=EPISODE, scopes=clearance)
    current = policy()
    no_evidence = resolve_injury_policy(deepcopy(row), policies=(current,), bank=BANK, phase=phase,
                                        readiness_decision=readiness)
    assert no_evidence["stage"] == "restore"
    with_evidence = resolve_injury_policy(deepcopy(row), policies=(current,), bank=BANK, phase=phase,
                                          readiness_decision=readiness, exposures=[event()])
    assert with_evidence["stage"] == "load"  # Only the episode's own evidence moved it.


# 9. Truncated history is never evidence of safety, and cannot be waived.
def test_truncated_history_blocks_and_cannot_be_removed_by_a_profile():
    assert progress([event()], history_truncated=True)["stage"] == "restore"
    with pytest.raises(ValueError, match="cannot remove a product safety requirement"):
        policy(remove=("complete_episode_history",))


# 10. Frozen prescriptions accepted under the pre-migration policies stay valid.
@pytest.mark.parametrize("policy_id,region,kind,stage", [
    ("chest_strain", "chest", "strain", "calm"), ("chest_strain", "chest", "strain", "restore"),
    ("ankle_sprain", "ankle", "sprain", "calm"), ("ankle_sprain", "ankle", "sprain", "restore")])
def test_frozen_snapshots_from_the_previous_file_are_not_held(policy_id, region, kind, stage):
    from tests.rehab_pathway_migration import get_rehab_bank
    row = dict(id=str(uuid4()), episode_id=str(uuid4()), athlete_id=str(uuid4()), canonical_location=region,
               body_region=region, body_area=f"Left {region}", description=f"{region} {kind}", side="left",
               injury_type=kind, severity="mild", status="monitoring" if stage == "restore" else "open",
               created_at="2026-09-28T00:00:00Z", updated_at="2026-09-28T00:00:00Z")
    if stage == "restore":
        row.update(latest_reported_status="improving", rehab_stage="restore")
    old = resolve_injury_policy(deepcopy(row), policies=load_clinical_policies(LEGACY), bank=get_rehab_bank())
    frozen = reconcile_session_prescription(None, decisions=[old], plan_id="p", training_day="2026-10-02",
                                            injuries=[row])
    assert frozen and frozen["session"]["blocks"]
    from tests.rehab_pathway_migration import composed
    new = resolve_injury_policy(deepcopy(row), policies=composed(), bank=get_rehab_bank())
    held = reconcile_session_prescription(None, decisions=[new], plan_id="p", training_day="2026-10-02",
                                          frozen=frozen, injuries=[row])
    assert held["safety_hold"] is False
    assert [b["policy_review_hash"] for b in frozen["session"]["blocks"]] == \
        [new["prescription"]["policy_review_hash"]] * len(frozen["session"]["blocks"])


# Families route; profiles activate.
UNPROFILED = [
    ("ankle", "hyperextension"), ("ankle", "swelling"), ("ankle", "pain"), ("ankle", "tightness"), ("chest", "sprain"),
    ("chest", "contusion"), ("chest", "tendonitis"), ("neck", "impingement"), ("knee", "hyperextension"),
    ("groin", "tendonitis"), ("triceps", "tendonitis"), ("wrist", "instability"), ("lower_back", "stiffness"), ("knee", "soreness"),
]


@pytest.mark.parametrize("region,kind", UNPROFILED)
def test_family_membership_alone_never_activates_a_region_and_type(region, kind):
    row = dict(id=INJURY, episode_id=EPISODE, athlete_id=str(uuid4()), canonical_location=region, body_region=region,
               body_area=region, description=f"{region} {kind}", side="left", injury_type=kind, severity="mild",
               status="monitoring", latest_reported_status="improving", rehab_stage="restore")
    decision = resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank())
    expected = "medical_review" if (region, kind) == ("ankle", "swelling") else "unsupported_prescription"
    assert decision["outcome"] == expected and decision["prescription"] is None
    assert decision["activation"] == "shadow"
    if expected == "medical_review":
        assert "urgent_injury_type" in decision["reason_codes"]
    else:
        assert decision["reason_codes"] == ["unsupported_injury_policy"]


def test_only_profiles_are_policies_and_families_carry_no_content():
    catalog = json.loads(PATHWAYS_PATH.read_text(encoding="utf-8"))
    assert {(p.region, p.injury_type) for p in load_clinical_policies()} == {
        ("chest", "strain"), ("ankle", "sprain"), ("hamstring", "strain"),
        ("calf", "strain"), ("groin", "strain"), ("quads", "strain"),
        ("biceps", "strain"), ("triceps", "strain"), ("shoulder", "strain"),
        ("ankle", "instability"), ("knee", "instability"), ("toe", "sprain"), ("wrist", "sprain"),
        ("elbow", "sprain"), ("shoulder", "sprain"), ("shoulder", "instability"), ("hand", "sprain"), ("fingers", "sprain"),
        *((region, "tendonitis") for region in ["achilles", "shoulder", "biceps", "forearm", "elbow", "wrist", "hand", "fingers"]),
        *((region, "impingement") for region in ["shoulder", "hip", "ankle", "elbow", "wrist"]),
        *((region, "hyperextension") for region in ["toe", "fingers", "elbow", "wrist", "hand", "shoulder"]),
        *((region, "contusion") for region in ["heel", "shin", "quads", "biceps", "triceps", "forearm", "shoulder", "elbow", "wrist", "hand", "fingers"]),
        *[('shoulder', 'pain'), ('elbow', 'pain'), ('wrist', 'pain'), ('hand', 'pain'), ('fingers', 'pain'), ('knee', 'pain'), ('hip', 'pain'), ('lower back', 'pain'), ('neck', 'stiffness'), ('elbow', 'stiffness'), ('wrist', 'stiffness'), ('lower back', 'stiffness'), ('neck', 'tightness'), ('shoulder', 'tightness'), ('neck', 'soreness'), ('shoulder', 'soreness')]}
    for family in catalog["families"]:
        assert set(family) <= {"family_id", "description", "injury_types", "transitions"}
    # Instability coverage requires its own regional profile and reviewed identities.
    policies = {p.policy_id: p for p in load_clinical_policies()}
    assert {p.region for p in policies.values() if p.injury_type == "instability"} == {"ankle", "knee", "shoulder"}
    assert not ({p.drill_id for p in policies["ankle_instability"].prescriptions}
                & {p.drill_id for p in policies["ankle_sprain"].prescriptions})
