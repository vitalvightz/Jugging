"""Source-bound equipment corrections do not grant prescription authority."""
from copy import deepcopy
import json

import pytest

from api.contracts.training_library import LibraryPreview, LibrarySearchResponse
from api.services import training_library as library
from fightcamp import equipment_requirements as requirements
from tests.support import _build_client


@pytest.fixture(scope="module")
def index():
    return library.LibraryCache().get()


def ids(result):
    return {entry["catalogue_id"] for entry in result["entries"]}


@pytest.mark.parametrize("ordinal", range(25))
def test_every_review_is_hash_bound_and_retains_raw_evidence(index, ordinal):
    identity, review = list(index.equipment_reviews.items())[ordinal]
    record = index.by_id[identity]
    detail = library.preview(record)
    projected = detail["equipment_requirements"]
    assert LibraryPreview.model_validate(detail).equipment_requirements is not None
    assert projected["source_value"] == detail["equipment_source"]["raw"] == review["source_value"]
    assert projected["row_revision"] == record["entry"]["row_sha256"] == review["row_sha256"]
    assert projected["source_pointer"] == record["entry"]["source_pointer"] + "/equipment"
    assert projected["audit_decisions"] == review["audit_decisions"]
    assert projected["evidence"] == review["evidence"]
    assert detail["prescription_allowed"] is False
    assert all(item["quantity"] is None for group in projected["groups"] for item in group["items"])
    changed = deepcopy(record["entry"])
    changed["row_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="Stale equipment"):
        requirements.project_requirements(changed, index.equipment_reviews)
    changed = deepcopy(record["entry"])
    changed["raw"]["equipment"] = ["unknown"]
    with pytest.raises(ValueError, match="Stale equipment"):
        requirements.project_requirements(changed, index.equipment_reviews)


@pytest.mark.parametrize("mode,items,available,expected", [
    ("ALL", ["sledgehammer", "tire"], set(), False),
    ("ALL", ["sledgehammer", "tire"], {"sledgehammer"}, False),
    ("ALL", ["sledgehammer", "tire"], {"tire"}, False),
    ("ALL", ["sledgehammer", "tire"], {"sledgehammer", "tire"}, True),
    ("ANY", ["treadmill", "rower"], {"rower"}, True),
    ("ANY", ["treadmill", "rower"], {"treadmill"}, True),
    ("ANY", ["treadmill", "rower"], {"bike_erg"}, False),
    ("OPTIONAL", ["handle_attachment"], set(), True),
    ("SINGLE", ["kettlebell"], {"kettlebell"}, True),
    ("SINGLE", ["kettlebell"], set(), False),
    ("ALL", ["bands", "partner"], {"bands"}, False),
    ("ALL", ["bands", "partner"], {"bands", "partner"}, True),
    ("UNKNOWN", [], {"bodyweight", "treadmill", "rower"}, False),
])
def test_full_partial_no_equipment_and_relationship_semantics(mode, items, available, expected):
    # ANY and OPTIONAL are supported, but no current source is assigned these
    # modes merely because a delimiter or conventional substitution suggests it.
    requirement = {"status": "reviewed", "groups": [{"mode": mode, "items": [{"canonical_id": item} for item in items]}]}
    assert requirements.equipment_setup_available(requirement, available) is expected


def test_optional_attachment_does_not_remove_mandatory_requirement():
    value = {"status": "reviewed", "groups": [
        {"mode": "SINGLE", "items": [{"canonical_id": "kettlebell"}]},
        {"mode": "OPTIONAL", "items": [{"canonical_id": "handle_attachment"}]},
    ]}
    assert not requirements.equipment_setup_available(value, {"handle_attachment"})
    assert requirements.equipment_setup_available(value, {"kettlebell"})
    assert not requirements.equipment_setup_available({"status": "unresolved", "groups": value["groups"]}, {"kettlebell"})
    assert not requirements.equipment_setup_available({}, {"kettlebell"})


def test_capability_token_does_not_prove_two_kettlebells_or_invalid_setup():
    pair = {"status": "reviewed", "groups": [{"mode": "SINGLE", "items": [{"canonical_id": "kettlebell", "quantity": 2}]}]}
    assert not requirements.equipment_setup_available(pair, {"kettlebell"})
    assert not requirements.equipment_setup_available({"status": "reviewed", "groups": [{"mode": "OPTIONAL", "items": []}]}, set())


def test_climbing_source_associations_are_corrected_in_effective_metadata(index):
    climbing = ids(library.search(index, filters={"equipment": "climbing_rope"}, limit=3000))
    assert len(climbing) == 2
    for identity in climbing:
        detail = library.preview(index.by_id[identity])
        assert detail["equipment"] == ["climbing_rope"]
        assert requirements.requirement_items(detail["equipment_requirements"]) == {"climbing_rope"}
        assert detail["equipment_source"]["raw"] == detail["equipment"]
    for value in ("battle_rope", "battle_ropes"):
        result = library.search(index, filters={"equipment": value}, limit=3000)
        assert result["total"] == 7 and not climbing & ids(result)
    for query in ("battle ropes", "battle_rope", "battle rope"):
        assert not climbing & ids(library.search(index, q=query, limit=3000))


def test_no_equipment_excludes_missing_apparatus_and_all_reviewed_compounds(index):
    no_equipment = ids(library.search(index, filters={"equipment": "no_equipment"}, limit=3000))
    assert not no_equipment & set(index.equipment_reviews)
    for name, apparatus in (("Towel Pull-Up Sprints", {"towel", "pullup_bar"}),
                            ("Slow-Lowered Pull-Up", {"pullup_bar"}),
                            ("Towel-assisted lateral neck isometric", {"towel"})):
        record = next(r for r in index.records if r["summary"]["name"] == name)
        assert record["facets"]["equipment"] == apparatus
        assert set(record["summary"]["equipment"]) == apparatus


def test_additional_mandatory_apparatus_visible_and_legacy_compound_filter_works(index):
    result = library.search(index, filters={"equipment": "sledgehammer/tire"}, limit=3000)
    assert result["total"] == 1
    row = result["entries"][0]
    assert row["name"] == "Rotational Sledgehammer Strike"
    assert row["equipment_requirements"]["groups"][0]["mode"] == "ALL"
    assert set(row["equipment"]) == {"sledgehammer", "tire"}
    identity = row["catalogue_id"]
    for gear in ("sledgehammer", "tire"):
        assert identity in ids(library.search(index, filters={"equipment": gear}, limit=3000))
    blocked = library.search(index, filters={"equipment": "sledgehammer/tire", "type": "conditioning"})
    assert blocked["total"] == 0
    valid = library.search(index, filters={"equipment": "sledgehammer/tire", "type": "strength_power"})
    assert valid["total"] == 1
    assert LibrarySearchResponse.model_validate(valid).entries[0].equipment_requirements is not None


def test_unknown_groups_are_not_split_or_inferred_and_remain_discoverable(index):
    for identity, review in index.equipment_reviews.items():
        if review["action"] != "REVIEW":
            continue
        record = index.by_id[identity]
        value = record["equipment_requirements"]
        assert value["status"] == "unresolved" and value["groups"] == [{"mode": "UNKNOWN", "items": []}]
        assert record["summary"]["equipment"] == [review["source_value"]]
        assert record["facets"]["equipment"] == {review["source_value"]}
        assert identity in ids(library.search(index, filters={"equipment": review["source_value"]}, limit=3000))


def test_unreviewed_entries_get_no_invented_requirements(index):
    for record in index.records:
        if record["entry"]["catalogue_id"] not in index.equipment_reviews:
            assert record["summary"]["equipment_requirements"] is None
            assert record["facets"] == library._prepare(record["entry"], projection=index.projection)["facets"]


def test_partner_is_explicit_and_not_apparatus(index):
    record = next(r for r in index.records if r["summary"]["name"] == "Reactive Band Taps (Partner)")
    items = record["equipment_requirements"]["groups"][0]["items"]
    assert {i["canonical_id"]: i["kind"] for i in items} == {"bands": "apparatus", "partner": "partner"}


def test_missing_reviewed_identity_fails_closed(monkeypatch):
    reviews = deepcopy(requirements.load_equipment_reviews())
    reviews["missing:fixture"] = next(iter(reviews.values()))
    monkeypatch.setattr(library, "load_equipment_reviews", lambda: reviews)
    with pytest.raises(ValueError, match="Missing reviewed equipment"):
        library.LibraryCache().get()


def test_api_returns_mandatory_setup_and_original_equipment_without_granting_prescription(index, monkeypatch):
    identity = next(identity for identity, r in index.by_id.items() if r["summary"]["name"] == "Towel Pull-Up Sprints")
    client, _, _ = _build_client()
    monkeypatch.setattr(library, "cache", library.LibraryCache())
    try:
        response = client.get("/api/training/library/entry", params={"catalogue_id": identity}, headers={"Authorization": "Bearer athlete-token"})
        assert response.status_code == 200
        body = response.json()
        assert body["equipment"] == ["pullup_bar", "towel"]
        assert body["equipment_source"]["raw"] == ["towel", "pullup_bar"]
        assert body["equipment_requirements"]["status"] == "reviewed"
        assert body["prescription_allowed"] is False
        assert body["selection_boundary"] == "training_validation_required"
        filtered = client.get("/api/training/library", params={"equipment": "no_equipment", "limit": 24}, headers={"Authorization": "Bearer athlete-token"})
        assert filtered.status_code == 200
        # #3166 adds three equipment-free core rows and retires one stance;
        # #3181 adds three footwork rows. Recount the exact current ID set.
        no_equipment_ids = {r["entry"]["catalogue_id"] for r in index.records
                            if r["summary"]["category"] == "training"
                            and "no_equipment" in r["facets"]["equipment"]}
        assert len(no_equipment_ids) == 280
        assert filtered.json()["total"] == len(no_equipment_ids)
        assert {entry["catalogue_id"] for entry in filtered.json()["entries"]} <= no_equipment_ids
        assert identity not in no_equipment_ids
        assert identity not in {entry["catalogue_id"] for entry in filtered.json()["entries"]}
    finally:
        client.close()


def test_review_contract_invalid_modes_duplicate_ids_and_unverified_items_fail(monkeypatch):
    contract = json.loads(requirements.REQUIREMENTS_FILE.read_bytes())
    for mutate in (lambda c: c["reviews"].append(deepcopy(c["reviews"][0])),
                   lambda c: c["reviews"][0]["groups"].append({"mode": "ANY", "items": []}),
                   lambda c: c["reviews"][0].update(evidence=""),
                   lambda c: c["reviews"][0].update(action="REVIEW")):
        invalid = deepcopy(contract)
        mutate(invalid)
        monkeypatch.setattr(requirements, "load_shared_contract", lambda _, value=invalid: value)
        with pytest.raises(ValueError):
            requirements.load_equipment_reviews()


