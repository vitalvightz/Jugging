"""The first Achilles clinical review cannot turn reported data into LOAD."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from api.contracts.achilles_restore_load import CRITERION_ID, REQUIRED_INPUTS, SOURCES, review_achilles_restore_load
from api.contracts.injury_policy import resolve_injury_policy
from api.contracts.rehab_assessment import AssessmentContext, read_assessment_input
from api.contracts.rehab_progression import evaluate_transition, resolve_reviewed_progression
from api.services.injury_episode_service import apply_episode_observations
from fightcamp.rehab_clinical import load_clinical_policies
from fightcamp.rehab_pathways import PathwayTransition
from fightcamp.rehab_protocols import get_rehab_bank
from tests.test_achilles_progression_inputs import NOW, assessment, capture, context as input_context, enriched


@pytest.fixture
def context():
    return input_context.__wrapped__()


def observed(context, **changes):
    return enriched(context, assessment(**{**dict(suspected_rupture=False, marked_weakness=False,
        traumatic_loss_of_function=False, clinician_restriction=False), **changes}))


def review(injury, **options):
    return review_achilles_restore_load(AssessmentContext.from_injury(injury, as_of=NOW, **options))


def policy():
    return next(p for p in load_clinical_policies() if p.policy_id == "achilles_tendonitis")


def review_transition():
    # Proposed code-reviewed checkpoint in an explicitly test-only transition.
    # No production declaration/activation is changed before the missing
    # clinical judgments, provenance and prescription can actually be supplied.
    return PathwayTransition(from_stage="restore", to_stage="load", requirements=[dict(
        requirement_id=CRITERION_ID, kind="functional_checkpoint", basis="clinical", checkpoint=CRITERION_ID,
        description="Individual applicability, function, symptoms and selected loading must be clinically reviewed.",
        sources=list(SOURCES))])


def test_all_four_available_inputs_are_not_clinical_readiness(context):
    injury = observed(context)
    ctx = AssessmentContext.from_injury(injury, as_of=NOW)
    assert all(read_assessment_input(key, ctx)["status"] == "pass" for key in REQUIRED_INPUTS)
    result = review(injury)
    assert result["status"] == "unknown" and result["promotion_allowed"] is False
    assert result["externally_verified"] is False
    assert {"achilles_verified_clinician_review_unavailable", "achilles_selected_load_functional_adequacy_not_captured",
        "achilles_acceptable_symptom_response_not_defined", "achilles_individual_load_prescription_not_defined"} <= set(result["reason_codes"])
    engine = evaluate_transition(review_transition(), policy=policy(), injury=injury, exposures=[], as_of=NOW)
    assert engine["status"] == "blocked" and engine["requirements"][0]["status"] == "unknown"
    assert engine["requirements"][0]["clinical_review"] == result


def test_unknown_subtype_fails_closed(context):
    result = review(observed(context, site="unknown"))
    assert result["subtype"] == "unknown" and result["status"] == "unknown"


def test_insertional_and_midportion_branches_do_not_share_cpg_applicability(context):
    insertional = review(observed(context, site="insertional"))
    midportion = review(observed(context, site="midportion"))
    assert insertional["subtype"] == "insertional" and midportion["subtype"] == "midportion"
    assert "achilles_insertional_unilateral_progression_requires_clinician_review" in insertional["reason_codes"]
    assert "achilles_midportion_structural_applicability_requires_clinician_review" in midportion["reason_codes"]
    assert insertional["reason_codes"] != midportion["reason_codes"]


@pytest.mark.parametrize("changes", [dict(injury_type="sprain"), dict(side="right"), dict(side="bilateral"),
    dict(episode_id="previous-episode"), dict(id="other-injury"), dict(athlete_id="other-athlete")])
def test_exact_attribution_is_required(context, changes):
    injury = observed(context)
    injury.update(changes)
    result = review(injury)
    assert result["status"] == "unknown" and result["observation_id"] is None


def test_newer_incomplete_snapshot_does_not_reuse_historical_assessment(context):
    injury = observed(context)
    newer = capture(context, assessment(heel_rise_completed=None, heel_rise_mode="unknown", heel_rise_quality="unknown",
        heel_rise_repetitions=None, heel_rise_assessor_usable=None))
    newer["created_at"] = "2026-10-05T18:00:00Z"
    injury["progression_assessments"].append(newer)
    assert review(injury)["reason_codes"] == ["achilles_input_or_source_insufficient"]


def test_later_setback_invalidates_earlier_ready_looking_assessment(context):
    injury = observed(context)
    result = review(injury, setback_at=datetime(2026, 10, 5, 17, tzinfo=timezone.utc))
    assert result["status"] == "unknown"
    assert result["reason_codes"] == ["assessment_observation_stale_or_future"]


@pytest.mark.parametrize("concern", ["suspected_rupture", "marked_weakness", "traumatic_loss_of_function",
    "clinician_restriction", "incompatible_pathology"])
def test_medical_or_restriction_hold_persists_despite_later_good_report(context, concern):
    bad = capture(context, assessment(**{concern: "suspected" if concern == "incompatible_pathology" else True}))
    good = capture(context, assessment())
    good["created_at"] = "2026-10-05T18:00:00Z"
    injury = apply_episode_observations(context[2], [bad, good], as_of=NOW)
    assert review(injury)["status"] == "fail"
    decision = resolve_injury_policy(injury, policies=load_clinical_policies(), bank=get_rehab_bank(), as_of=NOW)
    assert decision["outcome"] == "medical_review" and decision["prescription"] is None


@pytest.mark.parametrize("changes", [dict(completed_sessions=999), dict(days_since_injury=999),
    dict(camp_phase="GPP"), dict(camp_phase="SPP"), dict(camp_phase="TAPER"),
    dict(rehab_completed=True), dict(clinician_clearance={"scopes": ["rehab", "training", "contact"]})])
def test_counts_time_phase_completion_and_clearance_are_not_measurements(context, changes):
    assert review({**context[2], **changes})["status"] == "unknown"


@pytest.mark.parametrize("assessor", ["self_reported", "coach_observed", "clinician_physio", "unknown"])
def test_reported_assessor_never_becomes_verified_clinical_evidence(context, assessor):
    result = review(observed(context, assessor=assessor))
    assert result["status"] == "unknown" and result["externally_verified"] is False
    assert result["promotion_allowed"] is False


def test_forged_verification_flag_is_invalid_not_a_pass(context):
    injury = observed(context)
    injury["progression_assessments"][0]["payload"]["externally_verified"] = True
    assert review(injury)["reason_codes"] == ["assessment_invalid_attribution_or_provenance"]


@pytest.mark.parametrize("changes", [dict(during_symptoms=0.0, delayed_symptoms=0.0, heel_rise_repetitions=1000),
    dict(during_symptoms=10.0, delayed_symptoms=10.0, heel_rise_repetitions=0),
    dict(resistance="external", resistance_kg=1000.0)])
def test_no_invented_pain_rep_or_resistance_cutoff(context, changes):
    assert review(observed(context, **changes))["status"] == "unknown"


def test_incomplete_history_and_explicit_replay(context):
    injury = observed(context)
    assert review(injury, history_truncated=True)["reason_codes"] == ["assessment_history_incomplete"]
    newer = capture(context, assessment(suspected_rupture=True))
    newer["created_at"] = "2026-10-05T18:00:00Z"
    before = datetime(2026, 10, 5, 17, tzinfo=timezone.utc)
    injury["progression_assessments"].append(newer)
    earlier = AssessmentContext.from_injury(injury, as_of=before)
    assert review_achilles_restore_load(earlier) == review_achilles_restore_load(earlier)
    assert review_achilles_restore_load(earlier)["status"] == "unknown"
    assert review(injury)["status"] == "fail"


def test_test_only_open_transition_still_cannot_promote_without_clinical_pass(context):
    proposed = policy().model_copy(update={"live_stages": ["calm", "restore", "load"], "transitions": [review_transition()]})
    result = resolve_reviewed_progression(observed(context), base_stage="restore", policy=proposed, exposures=[], as_of=NOW)
    assert result["stage"] == "restore" and result["next_transition"]["status"] == "blocked"
    unrelated = next(p for p in load_clinical_policies() if p.policy_id == "elbow_tendonitis")
    assert evaluate_transition(review_transition(), policy=unrelated, injury=observed(context), exposures=[],
        as_of=NOW)["requirements"][0]["status"] == "missing_input"


def test_actual_load_prescription_and_other_profiles_remain_unchanged(context):
    before = resolve_injury_policy(context[2], policies=load_clinical_policies(), bank=get_rehab_bank(), as_of=NOW)
    after = resolve_injury_policy(observed(context), policies=load_clinical_policies(), bank=get_rehab_bank(), as_of=NOW)
    assert before["prescription"] == after["prescription"]
    assert after["achilles_load_review"]["status"] == "unknown"
    for p in load_clinical_policies():
        if p.policy_id in {"achilles_tendonitis", "elbow_tendonitis", "ankle_sprain", "shoulder_tendonitis"}:
            assert p.live_stages == ["calm","restore","load"] and not any(t.promotable for t in p.transitions[1:])
            continue
        assert set(p.live_stages) <= {"calm", "restore"}
        assert not any(t.promotable for t in p.transitions)
        assert not any(rx.stage in {"load", "dynamic", "return"} for rx in p.prescriptions)
    other = deepcopy(context[2])
    other.update(body_region="elbow", body_area="Left elbow", description="Elbow tendonitis")
    assert "achilles_load_review" not in resolve_injury_policy(other, policies=load_clinical_policies(), bank=get_rehab_bank(), as_of=NOW)


def test_all_64_main_profile_hashes_bank_and_archive_are_preserved(tmp_path):
    root = Path(__file__).resolve().parents[1]
    baseline = json.loads((root / "tests/fixtures/achilles_restore_load_baseline.json").read_text(encoding="utf-8"))
    from tools.rehab_metadata_review_lib import before_achilles_load_activation, before_elbow_content_addition
    bank, ledger = before_elbow_content_addition(
        json.loads((root / "data/rehab_bank.json").read_text(encoding="utf-8")),
        json.loads((root / "data/rehab_metadata_review.json").read_text(encoding="utf-8")))
    pathways = before_achilles_load_activation(
        json.loads((root / "data/rehab_pathways.json").read_text(encoding="utf-8")))
    dated_path = tmp_path / "dated_pathways.json"
    dated_path.write_text(json.dumps(pathways), encoding="utf-8")
    # #2984/#2986 intentionally republished profiles and bank content. Apply
    # the same hash-checked dated projection to policies and file fingerprints;
    # the immutable baseline must still detect any unaccounted-for drift.
    dated_policies = load_clinical_policies(dated_path, bank=bank)
    assert len(baseline["profile_hashes"]) == 64
    assert {p.policy_id: p.content_hash for p in dated_policies if p.policy_id not in {"achilles_tendonitis", "elbow_tendonitis", "ankle_sprain", "shoulder_tendonitis"}} == {
        k:v for k,v in baseline["profile_hashes"].items() if k not in {"achilles_tendonitis", "elbow_tendonitis", "ankle_sprain", "shoulder_tendonitis"}}
    for name, expected in baseline["file_hashes"].items():
        actual = (root / "data" / name).read_text(encoding="utf-8")
        if name in {"rehab_bank.json", "rehab_metadata_review.json"}:
            actual = json.dumps(bank if name == "rehab_bank.json" else ledger, indent=2, ensure_ascii=False) + "\n"
        if name == "rehab_pathways.json":
            actual = json.dumps(pathways,indent=2,ensure_ascii=False)+"\n"
        assert hashlib.sha256(actual.encode()).hexdigest() == expected, name
