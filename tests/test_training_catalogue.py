"""Source identity, evidence and discovery/prescription separation."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from copy import deepcopy
import json
import shutil

import pytest

from fightcamp import training_catalogue as catalogue
from tools.audit_training_library import SOURCES, audit
from tools.report_training_catalogue import coverage_report


@pytest.fixture(scope="module")
def entries():
    return catalogue.build_catalogue()


@pytest.fixture
def data_copy(tmp_path):
    for source in (*SOURCES, catalogue.MANIFEST_FILE, catalogue.ALIASES_FILE):
        target = tmp_path / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(catalogue.DATA_DIR / source, target)
    return tmp_path


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")


def test_all_audited_entries_accounted_for_with_unique_source_scoped_ids(entries):
    baseline = audit(catalogue.DATA_DIR)
    assert tuple(catalogue.SOURCE_CLASSES) == SOURCES
    # Recount real rows: #3166 adds 15 core exercises and retires one stance
    # hold; #3181 adds three technical footwork drills. Keep identity checks.
    assert len(entries) == baseline["total_entries"] == 2906
    assert Counter(e["source"] for e in entries) == {
        bank["source"]: bank["entries"] for bank in baseline["banks"] if bank["entries"]
    }
    assert len({e["catalogue_id"] for e in entries}) == len(entries)
    for entry in entries:
        assert entry["catalogue_id"] == f'{entry["source"]}:{entry["source_id"]}'
        assert entry["source_id"]
        if entry["identity_origin"] in {"id", "key"}:
            assert entry["source_id"] == entry["raw"][entry["identity_origin"]]


def test_identity_is_stable_across_reordering_and_json_layout(entries, data_copy):
    for source in SOURCES:
        payload = json.loads((data_copy / source).read_bytes())
        if isinstance(payload, list):
            payload.reverse()
            if source == "rehab_bank.json":
                for group in payload:
                    group["drills"].reverse()
        _write(data_copy / source, payload)
    reordered = catalogue.build_catalogue(data_copy)
    assert {e["catalogue_id"]: e["raw"] for e in entries} == {
        e["catalogue_id"]: e["raw"] for e in reordered
    }


def test_source_integrity_and_coverage_are_independent_of_checkout_line_endings(data_copy):
    for source in SOURCES:
        path = data_copy / source
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    lf_entries = catalogue.build_catalogue(data_copy)
    lf_report = coverage_report(data_copy)
    for source in SOURCES:
        path = data_copy / source
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert catalogue.build_catalogue(data_copy) == lf_entries
    assert coverage_report(data_copy) == lf_report
    assert catalogue.source_hash(b'{"name":"Original"}\r\n') != catalogue.source_hash(b'{"name":"Edited"}\r\n')


def test_metadata_edit_requires_reviewed_rebinding_not_a_new_identity(entries, data_copy):
    source = "exercise_bank.json"
    payload = json.loads((data_copy / source).read_bytes())
    original = payload[0]
    old_hash = catalogue.content_hash(original)
    old_id = next(e["catalogue_id"] for e in entries if e["source"] == source and e["row_sha256"] == old_hash)
    original["name"] = "Reviewed trap bar name"
    original["notes"] = "Reviewed cue change"
    _write(data_copy / source, payload)
    with pytest.raises(ValueError, match="Missing stable identity"):
        catalogue.build_catalogue(data_copy)
    # Report still counts the entry and denies normal-workout selection.
    unresolved = next(e for e in catalogue.build_catalogue(data_copy, strict=False) if e["name"] == original["name"])
    assert unresolved["catalogue_id"] is None
    assert not unresolved["eligibility"]["normal_workout_candidate"]
    assert coverage_report(data_copy)["without_stable_ids"] == 1
    manifest = json.loads((data_copy / catalogue.MANIFEST_FILE).read_bytes())
    binding = manifest["sources"][source].pop(old_hash)
    binding["name"] = original["name"]
    manifest["sources"][source][catalogue.content_hash(original)] = binding
    _write(data_copy / catalogue.MANIFEST_FILE, manifest)
    rebound = next(e for e in catalogue.build_catalogue(data_copy) if e["name"] == original["name"])
    assert rebound["catalogue_id"] == old_id


def test_missing_removed_and_duplicate_identities_fail_closed(data_copy):
    source = "exercise_bank.json"
    payload = json.loads((data_copy / source).read_bytes())
    _write(data_copy / source, payload[:-1])
    with pytest.raises(ValueError, match="Stale catalogue manifest"):
        catalogue.build_catalogue(data_copy)
    _write(data_copy / source, payload + [deepcopy(payload[0])])
    with pytest.raises(ValueError, match="Ambiguous stable catalogue identity"):
        catalogue.build_catalogue(data_copy)


def test_duplicate_names_and_media_variants_remain_distinct(entries):
    names = defaultdict(list)
    for entry in entries:
        names[" ".join(entry["name"].casefold().split())].append(entry)
    repeated = [rows for rows in names.values() if len(rows) > 1]
    # #2980 renamed the published ankle CAR option; one duplicate-name group
    # disappeared. Exact distinct identity/media checks remain below.
    assert len(repeated) == 57
    for rows in repeated:
        assert len({e["catalogue_id"] for e in rows}) == len(rows)
    rehab_duplicates = [rows for rows in repeated if all(e["source"] == "rehab_bank.json" for e in rows)]
    assert rehab_duplicates
    for rows in rehab_duplicates:
        assert all(e["raw_group"] for e in rows)
    variants = [e for e in entries if e["name"] in {"Box Jump (Max Height)", "Box Jump (Stick Landing)"}]
    assert len(variants) == 2
    assert len({e["exercise_key"] for e in variants}) == 2


@pytest.mark.parametrize("row,state", [
    ({}, "absent"), ({"equipment": None}, "null"),
    ({"equipment": []}, "known"), ({"equipment": "none"}, "known"),
    ({"equipment": "unknown"}, "unknown"), ({"equipment": ["unknown"]}, "unknown"),
    ({"equipment": ["band", "unknown"]}, "ambiguous"),
    ({"equipment": ""}, "empty"), ({"equipment": [None]}, "ambiguous"),
    ({"equipment": ["band"]}, "known"),
])
def test_unknown_equipment_never_becomes_no_equipment(row, state):
    assert catalogue.metadata_state(row, "equipment") == state


def test_classification_and_selection_boundaries(entries):
    assert Counter(e["content_class"] for e in entries) == {
        "strength_power": 359, "conditioning": 601, "technical_coordination": 86,
        "rehabilitation": 1514, "care_instruction": 104,
        "tactical_educational": 183, "visualisation": 59,
    }
    for entry in entries:
        eligibility = entry["eligibility"]
        assert eligibility["prescription_allowed"] is False
        if entry["content_class"] == "rehabilitation":
            assert eligibility["boundary"] == "rehab_pathway_only"
            assert not eligibility["normal_workout_candidate"]
            assert "fightcamp.rehab_selector.filter_rehab_candidates" in eligibility["validation_authorities"]
        elif entry["content_class"] not in catalogue.PHYSICAL_CLASSES:
            assert eligibility["boundary"] == "excluded"
            assert not eligibility["physical_candidate"]
            assert entry["exercise_key"] is None
        else:
            assert eligibility["normal_workout_candidate"]
            assert eligibility["boundary"] == "training_validation_required"
            assert "fightcamp.injury_guard.injury_decision" in eligibility["validation_authorities"]
    assert all(e["raw_group"]["type"] in catalogue.SURFACE_TISSUE_TYPES for e in entries if e["content_class"] == "care_instruction")


def test_reviewed_aliases_are_evidence_backed_and_context_specific(entries):
    aliases = {e["name"]: {a["label"] for a in e["aliases"]} for e in entries}
    assert {e["name"] for e in entries if "Punching power" in aliases[e["name"]]} == {
        "Medicine Ball Hook Throw", "Standing Cable Punch Simulation",
    }
    assert "Punching power" not in aliases["Seated Russian Twist (Band-Resisted)"]
    assert "Stamina" not in aliases["Assault Bike Sprint Intervals"]
    for entry in entries:
        for alias in entry["aliases"]:
            evidence = alias["evidence"]
            assert evidence["row_sha256"] == entry["row_sha256"]
            assert evidence["rationale"]
            if "field" in evidence:
                assert evidence["token"] in catalogue.strings(entry["raw"][evidence["field"]])
    labels = {a["label"] for e in entries for a in e["aliases"]}
    assert labels == {"Stamina", "Core stability", "Reaction speed", "Shoulder control", "Punching power"}


def test_entry_alias_does_not_survive_unreviewed_context_change(entries):
    entry = deepcopy(next(e for e in entries if e["name"] == "Medicine Ball Hook Throw"))
    entry["row_sha256"] = "changed"
    rules = json.loads((catalogue.DATA_DIR / catalogue.ALIASES_FILE).read_bytes())
    assert not any(a["label"] == "Punching power" for a in catalogue.search_aliases(entry, rules))


def test_metadata_provenance_raw_restrictions_and_no_invented_defaults(entries):
    sources = {source: json.loads((catalogue.DATA_DIR / source).read_bytes()) for source in SOURCES}
    for entry in entries:
        raw = sources[entry["source"]]
        for part in entry["source_pointer"].strip("/").split("/"):
            raw = raw[int(part)] if isinstance(raw, list) else raw[part]
        assert entry["raw"] == raw
        assert entry["row_sha256"] == catalogue.content_hash(raw)
        for field, evidence in entry["metadata"].items():
            if field in raw:
                assert evidence["value"] == raw[field]
                assert evidence["provenance"]["pointer"] == f'{entry["source_pointer"]}/{field}'
                assert evidence["provenance"]["source_sha256"] == entry["source_sha256"]
            else:
                assert evidence == {"state": "absent"}
        for piece in entry["search_evidence"]:
            assert piece["text"] in catalogue.strings(raw[piece["field"]])
        assert entry["metadata"]["difficulty"]["state"] != "known"
    # Known biomechanical metadata is not a current prescription or clearance.
    reviewed = next(e for e in entries if e["source_id"] == "ankle_sprain_single_leg_balance_on_foam_pad")
    assert reviewed["metadata"]["rehab_stage"]["value"] == "load"
    assert not reviewed["eligibility"]["prescription_allowed"]
    assert reviewed["raw_group"]["type"] == "sprain"


def test_catalogue_is_non_mutating_and_does_not_change_bank_consumers(entries):
    owned = catalogue.build_catalogue()
    owned[0]["raw"]["name"] = "consumer mutation"
    assert catalogue.build_catalogue()[0]["raw"]["name"] != "consumer mutation"


def test_catalogue_use_does_not_change_prescribed_training_outputs():
    from fightcamp.main import generate_plan
    from support import _build_request

    payload = _build_request().to_payload()
    payload["random_seed"] = 7
    before = asyncio.run(generate_plan(deepcopy(payload)))
    catalogue.build_catalogue()
    after = asyncio.run(generate_plan(deepcopy(payload)))
    assert before["plan_text"] and before["plan_text"] == after["plan_text"]
    assert before["stage2_payload"] == after["stage2_payload"]
