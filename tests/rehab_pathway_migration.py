"""Dated chest/ankle family-composition migration, rebuilt for regression tests.

Shared by the pathway safety-invariant and prescription-bundle suites, which
pin the original migration rather than later versioned activations.
"""
from __future__ import annotations

import json
from pathlib import Path

from fightcamp.rehab_clinical import compose_policy
from fightcamp.rehab_protocols import get_rehab_bank as current_rehab_bank

ROOT = Path(__file__).resolve().parents[1]
MIGRATED_POLICY_IDS = {"chest_strain", "ankle_sprain"}


def get_rehab_bank():
    """The bank as of the migration, before later source-backed publications."""
    from tools.rehab_metadata_review_lib import before_bank_publication
    return before_bank_publication(current_rehab_bank(), "drills")


def composed():
    """Chest/ankle policies composed from the family catalogue at migration time.

    Only the manifest-pinned activation delta is subtracted; current ankle
    behaviour is covered by test_ankle_load_activation.
    """
    from fightcamp.rehab_pathways import PathwayCatalog
    from tools.rehab_metadata_review_lib import before_achilles_load_activation
    raw = json.loads((ROOT / "data/rehab_pathways.json").read_text(encoding="utf-8"))
    catalog = PathwayCatalog.model_validate(before_achilles_load_activation(raw))
    return tuple(compose_policy(catalog, p, bank=get_rehab_bank())
                 for p in catalog.profiles if p["policy_id"] in MIGRATED_POLICY_IDS)
