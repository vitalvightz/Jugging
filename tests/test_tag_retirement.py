"""Vocabulary cleanup must preserve programme and historical tag semantics."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import subprocess
import sys

import pytest

from fightcamp.tag_vocabulary import read_tag_vocabulary_items
from fightcamp.tagging import normalize_tag, normalize_tags
from fightcamp.training_catalogue import content_hash
from tools import check_tag_retirement as check
from tools.audit_tag_registry import audit_registry
from tools.injury_tag_authority import collect_generated_injury_tags
from tools import validate_banks

PLAN_FIXTURE = check.ROOT / "tests/fixtures/tag_retirement_plans.json"
# Reviewed canonical tags registered after the retirement (data/tag_registry_review.json).
ADDED_AFTER_RETIREMENT = frozenset({"anti_lateral_flexion"})


@pytest.fixture(scope="module")
def review():
    return json.loads(check.REVIEW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def registry():
    return audit_registry()


def test_each_original_finding_has_one_evidence_backed_classification(review):
    original = json.loads(check.REGISTER.read_text(encoding="utf-8"))["issues"]
    classified = review["findings"]
    assert check.digest(check.REGISTER) == review["original_register_sha256"]
    assert len(classified) == len(original) == 196
    assert {row["issue_id"] for row in classified} == {row["issue_id"] for row in original}
    assert all(row["rationale"] and row["consumer_evidence"] for row in classified)
    assert all(row["disposition"] == "preserve" for row in classified
               if row["classification"] == "intentional_distinction")
    mixed = next(row for row in classified if row["issue_id"] == "MI-9e5b23ccad9d")
    assert mixed["classification"] == "requires_investigation"
    assert mixed["disposition"] == "partial_vocabulary_retirement_only"


def test_only_reviewed_declarations_are_removed_in_original_order(review):
    original = review["original_vocabulary"]
    retired = set(review["retired_tags"])
    current = read_tag_vocabulary_items(check.VOCABULARY)
    assert len(original) == 355
    assert len(retired) == 16
    # Canonical tags reviewed and registered after this retirement review.
    assert set(current) - set(original) == ADDED_AFTER_RETIREMENT
    current = [tag for tag in current if tag not in ADDED_AFTER_RETIREMENT]
    assert current == [tag for tag in original if tag not in retired]
    assert len(current) == 339
    assert set(original) - set(current) == retired


def test_all_banks_clinical_reviews_aliases_and_historical_identities_are_unchanged(review):
    for source, expected in review["protected_source_sha256"].items():
        assert check.digest(check.ROOT / source) == expected, source


def test_retired_tags_have_no_active_or_historical_identifier_consumer(review, registry):
    references = check.token_references(set(review["retired_tags"]), check.reference_paths())
    assert check.retirement_failures(review, registry, references) == []
    # Head/side kicks are still legitimate coaching prose, kept byte-for-byte.
    assert {tag: rows for tag, rows in references.items() if rows} == review["retained_prose_references"]


def test_new_identifier_and_legacy_consumers_block_retirement(tmp_path, review, registry):
    source = tmp_path / "new_consumer.py"
    source.write_text("tags = ['axe_kick', 'back kick', 'head-kick']\n", encoding="utf-8")
    references = check.token_references(set(review["retired_tags"]), [source])
    failures = check.retirement_failures(review, registry, references)
    assert any("axe_kick" in failure for failure in failures)
    assert any("back_kick" in failure for failure in failures)
    assert any("head_kick" in failure for failure in failures)


def test_absent_bank_occurrence_cannot_retire_generated_safety_authority(review, registry):
    tag = "achilles_high_risk_impact"
    assert tag not in registry["bank_tag_details"]
    assert tag in collect_generated_injury_tags()
    assert tag in read_tag_vocabulary_items(check.VOCABULARY)
    unsafe = deepcopy(review)
    unsafe["retired_tags"] = [tag]
    assert any("active tag authority" in failure for failure in check.retirement_failures(unsafe, registry, {}))


def test_bank_validation_findings_are_identical_with_old_and_retained_vocabulary(review, monkeypatch):
    before, after = [], []
    current_status = validate_banks.run_validation("strict", emit=after.append)
    with monkeypatch.context() as original:
        original.setattr(validate_banks, "read_tag_vocabulary_items",
                         lambda _path: [*review["original_vocabulary"], *sorted(ADDED_AFTER_RETIREMENT)])
        original_status = validate_banks.run_validation("strict", emit=before.append)
    assert current_status == original_status
    # Only the explicitly intended allowlist-size message may change.
    loaded = 355 + len(ADDED_AFTER_RETIREMENT)
    assert after == [line.replace(f"Tag vocabulary loaded: {loaded} tags",
                                  f"Tag vocabulary loaded: {339 + len(ADDED_AFTER_RETIREMENT)} tags")
                     for line in before]


def test_legacy_normalization_and_intentional_programming_distinctions_survive(review):
    for tag in review["original_vocabulary"]:
        assert normalize_tag(tag) == tag
    for tag in review["retired_tags"]:
        assert normalize_tag(tag.replace("_", " ")) == tag
        assert normalize_tag(tag.replace("_", "-").upper()) == tag
    for tags in (
        ["reaction", "reactive", "mech_reactive"],
        ["grip", "mech_grip_support", "mech_grip_intensive", "mech_grip_crush"],
        ["mech_acceleration", "mech_deceleration"],
        ["no_d4_to_d1", "no_d7_to_d1"],
        ["hand_foot_timing", "head_foot_timing", "foot_hand_timing"],
    ):
        assert normalize_tags(tags) == tags


def test_injury_stages_and_reviewed_progression_match_pre_retirement_decisions():
    from api.contracts.rehab_progression import resolve_reviewed_progression
    from api.contracts.rehab_stage import resolve_rehab_stage
    from fightcamp.rehab_clinical import load_clinical_policies

    fixture = json.loads(PLAN_FIXTURE.read_text(encoding="utf-8"))
    for case in fixture["rehab_stages"]:
        assert resolve_rehab_stage(case["injury"]).as_dict() == case["expected"]
    policy = load_clinical_policies()[0]
    for case in fixture["reviewed_progression"]:
        actual = resolve_reviewed_progression(
            **case["kwargs"], policy=policy,
            as_of=datetime(2026, 10, 11, 9, tzinfo=timezone.utc),
        )
        assert actual == case["expected"], case["case"]


def test_complete_current_plans_match_pre_retirement_selection_dose_and_personalisation(tmp_path):
    fixture = json.loads(PLAN_FIXTURE.read_text(encoding="utf-8"))
    output = tmp_path / "plans.json"
    subprocess.run(
        [sys.executable, str(check.ROOT / "tools/canonical_metadata_plan_probe.py"),
         str(PLAN_FIXTURE), str(output)], cwd=check.ROOT,
        env={**os.environ, "PYTHONHASHSEED": fixture["python_hash_seed"]},
        check=True, capture_output=True, timeout=300,
    )
    actual = json.loads(output.read_text(encoding="utf-8"))
    assert len(actual) == len(fixture["plans"]) == 6
    for before, after in zip(fixture["plans"], actual):
        assert before["case"] == after["case"]
        # Full ordered outputs, not just chosen names or aggregate coverage.
        assert content_hash(after["plan_text"]) == before["plan_text_sha256"], before["case"]
        assert content_hash(after["stage2_payload"]) == before["stage2_sha256"], before["case"]
