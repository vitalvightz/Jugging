"""Unpublished metadata cannot prescribe rehab; live policies retain authority.

#2984 removed legacy generation fallback. Empty-policy cases protect that
boundary even when the bank metadata changes. Positive cases use the shipped
published ankle policy and keep dose, identity and single-drill coverage.
"""

from __future__ import annotations

import copy

import pytest

from fightcamp.rehab_clinical import load_clinical_policies
from fightcamp.stage2_payload import _build_rehab_slots
from fightcamp import rehab_protocols
from fightcamp.rehab_protocols import (
    SURFACE_WOUND_CARE_NOTE,
    classify_drill_function,
    generate_rehab_protocols,
    get_rehab_bank,
    match_drill_function,
)
from fightcamp.rehab_schema import MSK_DRILL_FIELDS, is_surface_injury_type

PHASES = ("GPP", "SPP", "TAPER")


@pytest.fixture(autouse=True)
def unpublished_policy_scope(monkeypatch):
    """#2984: bank metadata alone is never prescription authority."""
    monkeypatch.setattr("fightcamp.rehab_clinical.load_clinical_policies", lambda: ())


@pytest.fixture
def restorable_bank():
    """Yield the live bank, restoring the original records afterwards."""
    original = copy.deepcopy(get_rehab_bank())
    rehab_protocols._REHAB_DRILLS_BY_ID_CACHE = None
    try:
        yield get_rehab_bank()
    finally:
        rehab_protocols._REHAB_BANK_CACHE = original
        # The identity index holds the same mutable drill objects. Restoring only
        # the bank leaves hostile metadata visible to later completion tests.
        rehab_protocols._REHAB_DRILLS_BY_ID_CACHE = None


def _protocol(injury: str, phase: str, **kwargs) -> str:
    text, _ = generate_rehab_protocols(
        injury_string=injury, exercise_data=[], current_phase=phase, **kwargs
    )
    return text


def _structured(location: str, injury_type: str, severity: str | None = None) -> list[dict]:
    return [
        {
            "canonical_location": location,
            "location": location,
            "rehab_type": injury_type,
            "injury_type": injury_type,
            "severity": severity,
        }
    ]


def _hostile_metadata(bank: list[dict]) -> None:
    """Stamp every musculoskeletal drill with metadata that would forbid it."""
    for entry in bank:
        if is_surface_injury_type(entry.get("type")):
            continue
        for drill in entry.get("drills", []):
            drill.update(
                {
                    "rehab_stage": "return",
                    "function": "tendon_loading",
                    "equipment": ["barbell"],
                    "dose": {"sets": 99, "reps": 99, "duration_seconds": 999},
                    "impact": "high",
                    "load": "high",
                    "velocity": "high",
                    "pain_ceiling": 0,
                    "allowed_severities": ["high"],
                    "progress_when": ["never"],
                    "regress_when": ["always"],
                    "stop_when": ["any pain at all"],
                }
            )


# ---------------------------------------------------------------------------
# Unpublished metadata does not grant selection
# ---------------------------------------------------------------------------


CASES = [
    ("ankle sprain", "ankle", "sprain", None),
    ("knee pain", "knee", "pain", "low"),
    ("shoulder impingement", "shoulder", "impingement", "moderate"),
    ("achilles tendonitis", "achilles", "tendonitis", "high"),
    ("lower back stiffness", "lower_back", "stiffness", None),
    ("hamstring strain", "hamstring", "strain", "moderate"),
]


@pytest.mark.parametrize("injury,location,injury_type,severity", CASES)
@pytest.mark.parametrize("phase", PHASES)
def test_hostile_metadata_does_not_change_the_rehab_block(
    restorable_bank, injury, location, injury_type, severity, phase
):
    parsed = _structured(location, injury_type, severity)
    before = _protocol(injury, phase, parsed_entries=parsed)

    _hostile_metadata(restorable_bank)
    after = _protocol(injury, phase, parsed_entries=parsed)

    assert after == before


@pytest.mark.parametrize("day_type", [None, "sparring", "strength", "aerobic", "recovery"])
def test_hostile_metadata_does_not_change_day_type_output(restorable_bank, day_type):
    parsed = _structured("knee", "tendonitis", "moderate")
    before = _protocol("knee tendonitis", "SPP", parsed_entries=parsed, day_type=day_type)

    _hostile_metadata(restorable_bank)

    assert _protocol("knee tendonitis", "SPP", parsed_entries=parsed, day_type=day_type) == before


def test_stripping_the_metadata_entirely_does_not_change_the_rehab_block(restorable_bank):
    """Legacy, unmigrated records still render exactly the same."""
    parsed = _structured("ankle", "sprain")
    before = _protocol("ankle sprain", "GPP", parsed_entries=parsed)

    for entry in restorable_bank:
        for drill in entry.get("drills", []):
            for field in (*MSK_DRILL_FIELDS, "id"):
                drill.pop(field, None)

    assert _protocol("ankle sprain", "GPP", parsed_entries=parsed) == before


def test_metadata_values_never_leak_into_the_rendered_block():
    text = _protocol("ankle sprain", "GPP", parsed_entries=_structured("ankle", "sprain"))

    assert text.strip()
    for marker in ("rehab_stage", "pain_ceiling", "allowed_severities", "duration_seconds", "ankle_sprain_"):
        assert marker not in text


# ---------------------------------------------------------------------------
# Publication and phase behaviour
# ---------------------------------------------------------------------------


def _live_ankle_protocol(monkeypatch, phase, **kwargs):
    policies = load_clinical_policies()
    policy = next(p for p in policies if p.policy_id == "ankle_sprain")
    assert policy.activation == "live" and policy.status == "active"
    monkeypatch.setattr("fightcamp.rehab_clinical.load_clinical_policies", lambda: policies)
    entry = {**_structured("ankle", "sprain", "moderate")[0],
             "rehab_stage": "restore", "laterality": "left",
             "id": "ankle-1", "episode_id": "episode-1", "athlete_id": "athlete-1",
             "available_equipment": ["stable_support"]}
    text = _protocol("ankle sprain", phase, parsed_entries=[entry], **kwargs)
    slots = _build_rehab_slots(text, phase)
    assert len(slots) == 1
    selected = slots[0]["selected"]
    approved = next(p for p in policy.prescriptions if p.drill_id == selected["rehab_drill_id"])
    assert approved.instructions in text
    assert approved.stage == "restore"
    return text, approved


@pytest.mark.parametrize("phase", PHASES)
def test_unpublished_bank_cannot_supply_drills_but_live_policy_can(monkeypatch, phase):
    text = _protocol("ankle sprain", phase, parsed_entries=_structured("ankle", "sprain"))
    assert "Ankle" in text and "No published rehab is available" in text
    assert _build_rehab_slots(text, phase) == []
    text, _approved = _live_ankle_protocol(monkeypatch, phase)
    assert "Ankle" in text and "•" in text and "[Function:" in text
    assert "Consult with a healthcare professional" not in text


def test_phase_notes_cannot_bypass_publication_or_replace_approved_instructions(restorable_bank, monkeypatch):
    """#2984: legacy phase notes grant no drill; approved notes stay intact."""
    originals = [(drill, drill["notes"]) for entry in restorable_bank
                 if entry["location"] == "ankle" and entry["type"] == "sprain"
                 for drill in entry["drills"]]
    for drill, _notes in originals:
        drill["notes"] = "GPP: Rebuild proprioception → SPP: Controlled sport movement → TAPER: Maintain comfortable range"
    for phase in ("GPP", "SPP"):
        text = _protocol("ankle sprain", phase, parsed_entries=_structured("ankle", "sprain"))
        assert "No published rehab is available" in text
        assert _build_rehab_slots(text, phase) == []
        assert "Rebuild proprioception" not in text and "Controlled sport movement" not in text
    for drill, notes in originals:
        drill["notes"] = notes
    gpp, approved = _live_ankle_protocol(monkeypatch, "GPP")
    spp, same = _live_ankle_protocol(monkeypatch, "SPP")
    assert same.drill_id == approved.drill_id
    assert approved.instructions in gpp and approved.instructions in spp


@pytest.mark.parametrize("phase", PHASES)
def test_each_phase_yields_its_own_block(phase):
    text = _protocol("knee tendonitis", phase, parsed_entries=_structured("knee", "tendonitis"))
    assert "Knee" in text


def test_sparring_day_keeps_publication_boundary_and_single_drill_ceiling(monkeypatch):
    text = _protocol(
        "knee tendonitis", "SPP", parsed_entries=_structured("knee", "tendonitis"), day_type="sparring"
    )
    assert "No published rehab is available" in text
    assert _build_rehab_slots(text, "SPP") == []
    text, _approved = _live_ankle_protocol(monkeypatch, "SPP", day_type="sparring")
    assert len([line for line in text.splitlines() if line.strip().startswith("•")]) == 1


def test_high_severity_still_filters_aggressive_drills():
    text = _protocol(
        "ankle instability", "SPP", parsed_entries=_structured("ankle", "instability", "high")
    )
    lowered = text.lower()
    for blocked in ("pogo", "depth jump", "hop-stick"):
        assert blocked not in lowered


def test_no_injury_still_short_circuits():
    assert "No rehab work required" in _protocol("", "GPP")


# ---------------------------------------------------------------------------
# Safety gates are untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("injury_type", ["cut", "laceration", "abrasion", "graze", "blister"])
def test_surface_injuries_stay_out_of_loading_rehab(injury_type):
    text = _protocol(
        f"knee {injury_type}", "GPP", parsed_entries=_structured("knee", injury_type)
    )

    assert "[Function:" not in text
    for loading in ("isometric", "eccentric", "balance", "calf raise"):
        assert loading not in text.lower()


def test_surface_wound_care_note_is_still_used_for_open_wounds():
    text = _protocol("knee cut", "GPP", parsed_entries=_structured("knee", "cut"))
    assert SURFACE_WOUND_CARE_NOTE in text


def test_minor_surface_injury_still_trains_through():
    from fightcamp.injury_registry import SURFACE_MINOR_TRAIN_THROUGH_NOTE

    text = _protocol("knee graze", "GPP", parsed_entries=_structured("knee", "graze"))
    assert SURFACE_MINOR_TRAIN_THROUGH_NOTE in text


def test_urgent_structured_injury_still_returns_the_red_flag_block():
    parsed = [
        {
            "canonical_location": "knee",
            "injury_type": "acl_tear",
            "rehab_type": "acl_tear",
            "flags": ["urgent", "structural_red_flag"],
            "triage_category": "suspected_ligament_tear",
        }
    ]
    text = _protocol("knee acl tear", "GPP", parsed_entries=parsed)

    assert "Red Flag Detected" in text
    assert "•" in text
    assert "[Function:" not in text


def test_red_flag_text_injury_still_blocks_rehab():
    text = _protocol("knee fracture", "GPP")

    assert "Red Flag Detected" in text
    assert "cleared by clinician" in text


# ---------------------------------------------------------------------------
# Legacy keyword classification remains deterministic
# ---------------------------------------------------------------------------


def test_match_drill_function_reports_ambiguity_instead_of_defaulting():
    assert match_drill_function("Banded Clamshell") == "activation"
    assert match_drill_function("Heel Walks") is None


def test_classify_drill_function_still_defaults_to_control():
    assert classify_drill_function("Heel Walks") == rehab_protocols.AMBIGUOUS_DRILL_FUNCTION
    assert classify_drill_function("Heel Walks") == "control"


def test_stored_function_matches_explicit_review_or_legacy_keyword_migration():
    """Explicit reviewed metadata is authoritative; legacy values match seeding."""
    # The autouse legacy scope disables activation, so read the actual policy data.
    import json
    from fightcamp.config import DATA_DIR
    pilot_ids = {p["drill_id"] for policy in json.loads((DATA_DIR / "rehab_pathways.json").read_text(encoding="utf-8"))["profiles"]
                  for p in policy["prescriptions"]}
    reviewed_functions = {r["drill_id"]: r["proposed"]["function"]
                          for r in json.loads((DATA_DIR / "rehab_metadata_review.json").read_text(encoding="utf-8"))
                          if r["review_state"] == "reviewed"}
    for entry in get_rehab_bank():
        if is_surface_injury_type(entry.get("type")):
            continue
        for drill in entry.get("drills", []):
            if drill.get("id") in reviewed_functions:
                assert drill["function"] == reviewed_functions[drill["id"]]
                continue
            if drill.get("id") in pilot_ids:
                # Pilot function metadata is documented explicitly, not inferred from names.
                continue
            declared = drill.get("function")
            if declared is None:
                assert match_drill_function(drill["name"], drill.get("notes", "")) is None
            else:
                assert declared == match_drill_function(drill["name"], drill.get("notes", ""))
