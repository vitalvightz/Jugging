"""Per-entry Goal evidence changes discovery, never prescription or lexical rank."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from api.contracts.training_library import LibraryPreview
from api.services import training_library as lib
from fightcamp import training_metadata as metadata
from fightcamp.tag_maps import GOAL_NORMALIZER
from fightcamp.training_catalogue import content_hash, source_hash
from tests.support import _build_client

ROOT = Path(__file__).resolve().parents[1]
REVIEWS = json.loads((ROOT / "shared/training-metadata.json").read_bytes())["catalogue_projection"]["goal_reviews"]


@pytest.fixture(scope="module")
def index():
    return lib.LibraryCache().get()


@pytest.mark.parametrize("review", REVIEWS, ids=lambda r: r["catalogue_id"])
def test_each_review_adds_a_supported_goal_filter_and_preview(index, review):
    record = index.by_id[review["catalogue_id"]]
    entry = record["entry"]
    before = lib._prepare(entry, projection={**index.projection, "goal_reviews": []}, equipment_reviews=index.equipment_reviews)
    assert not before["facets"]["goal"]
    assert review["canonical_id"] in set(GOAL_NORMALIZER.values())
    assert record["facets"]["goal"] == {review["label"]}
    assert review["catalogue_id"] in {r["catalogue_id"] for r in lib.search(index, filters={"goal": review["label"]}, limit=3000)["entries"]}
    for field in ("type", "sport", "equipment", "body_focus"):
        assert record["facets"][field] == before["facets"][field]
        for value in record["facets"][field]:
            assert review["catalogue_id"] in {r["catalogue_id"] for r in lib.search(index, filters={"goal": review["label"], field: value}, limit=3000)["entries"]}
    for field in ("aliases", "metadata", "description", "words", "discovery"):
        assert record[field] == before[field]
    detail = LibraryPreview.model_validate(lib.preview(record)).model_dump()
    evidence = next(v for v in detail["metadata_projection"] if v["status"] == "reviewed_goal")
    expected = {f: entry["raw"][f] for f in review["evidence_fields"]}
    if review.get("programming_evidence"):
        expected["programming_context"] = review["programming_evidence"]
        from fightcamp.strength import _classify_prescription_type, _prescription_templates
        context = review["programming_evidence"]
        assert context["source_sha256"] == source_hash((ROOT / context["source"]).read_bytes())
        assert context["archetype"] == _classify_prescription_type(entry["raw"])
        assert context["dose"] == _prescription_templates(context["phase"])[context["archetype"]]
    assert evidence["goal_evidence"] == expected
    assert evidence["row_revision"] == content_hash(entry["raw"]) == review["row_sha256"]
    assert evidence["source_pointer"] == entry["source_pointer"] + "/" + review.get("source_field", "why")
    assert evidence["association_note"] == review["rationale"]
    assert detail["focus"] == [review["label"]]
    assert detail["prescription_allowed"] is False


def test_exact_new_coverage_and_no_other_facet_or_category_changes(index):
    base = json.loads((ROOT / "tools/canonical_metadata_baseline.json").read_bytes())
    assert base["goal_filter_entries"] == 222
    # Rows added after the baseline take Goal facets from existing source rules;
    # this pins only the reviewed changes to baseline rows.
    baseline_records = [r for r in index.records if r["entry"]["catalogue_id"] in base["original_facets"]]
    # No baseline row may drop out of the comparison unnoticed.
    assert len(baseline_records) == len(base["original_facets"]) == 2855
    added = [r for r in index.records if r["entry"]["catalogue_id"] not in base["original_facets"]]
    # Added rows carry no unreviewed explicit goals.
    assert added and not [r["entry"]["catalogue_id"] for r in added if r["entry"]["raw"].get("goals")]
    changed = {r["entry"]["catalogue_id"] for r in baseline_records if sorted(r["facets"]["goal"]) != base["original_facets"][r["entry"]["catalogue_id"]]["goal"]}
    assert changed == {r["catalogue_id"] for r in REVIEWS}
    assert len(changed) == 56
    assert sum(bool(r["facets"]["goal"]) for r in baseline_records if r["summary"]["category"] == "training") == 278


@pytest.mark.parametrize("raw", [
    {"tags": ["mech_grip_support"]}, {"tags": ["mech_reactive"]},
    {"tags": ["mech_ballistic"]}, {"system": "aerobic"},
    {"name": "Explosive Power Grip Strength Reaction Time", "equipment": ["medicine_ball"]},
    {"qualities": ["balance", "limb_independence"], "method": "plyometric"},
])
def test_misleading_metadata_does_not_prove_goals(index, raw):
    entry = deepcopy(index.by_id[REVIEWS[0]["catalogue_id"]]["entry"])
    entry.update(catalogue_id="unreviewed-training", aliases=[])
    entry["raw"] = {"name": "Unreviewed", "equipment": [], **raw}
    entry["metadata"] = {f: {"state": "known" if f in entry["raw"] else "absent"} for f in entry["metadata"]}
    record = lib._prepare(entry, projection=index.projection)
    assert not record["facets"]["goal"]
    isolated = lib.LibraryIndex((), "fixture", (record,), {}, index.projection)
    for goal in ("Grip strength", "Reaction speed", "Power", "Stamina", "Coordination", "Balance"):
        assert lib.search(isolated, filters={"goal": goal})["total"] == 0


def test_stale_wrong_category_and_missing_identity_fail_closed(index, monkeypatch):
    entry = deepcopy(index.by_id[REVIEWS[0]["catalogue_id"]]["entry"])
    entry["row_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="Stale Goal review"):
        metadata.project_entry(entry, index.projection)
    entry["content_class"] = "rehabilitation"
    with pytest.raises(ValueError, match="only to Training"):
        metadata.project_entry(entry, index.projection)
    projection = deepcopy(index.projection)
    projection["goal_reviews"][0]["catalogue_id"] = "missing"
    monkeypatch.setattr(lib, "load_projection", lambda: projection)
    with pytest.raises(ValueError, match="Missing reviewed Goal identities"):
        lib.LibraryCache().get()


def test_authenticated_api_exposes_reviewed_goal_and_raw_evidence(monkeypatch):
    client, _, _ = _build_client()
    monkeypatch.setattr(lib, "cache", lib.LibraryCache())
    review = REVIEWS[0]
    try:
        auth = {"Authorization": "Bearer athlete-token"}
        result = client.get("/api/training/library", params={"goal": review["label"], "limit": 50}, headers=auth)
        assert result.status_code == 200
        assert review["catalogue_id"] in {r["catalogue_id"] for r in result.json()["entries"]}
        response = client.get("/api/training/library/entry", params={"catalogue_id": review["catalogue_id"]}, headers=auth)
        assert response.status_code == 200
        detail = response.json()
        assert detail["focus"] == [review["label"]]
        assert next(v for v in detail["metadata_projection"] if v["status"] == "reviewed_goal")["goal_evidence"]["cue"]
    finally:
        client.close()
