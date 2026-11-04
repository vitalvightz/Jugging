"""Vocabulary cleanup must preserve programme and historical tag semantics."""
from copy import deepcopy
import json

import pytest

from fightcamp.tag_vocabulary import read_tag_vocabulary_items
from fightcamp.tagging import normalize_tag, normalize_tags
from tools import check_tag_retirement as check
from tools.audit_tag_registry import audit_registry
from tools.injury_tag_authority import collect_generated_injury_tags

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


