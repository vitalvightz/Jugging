"""Region-aware Rehab/Prehab labelling for a plan's rehab blocks.

Rehab work stays named "Rehab" only while the body region it targets is actually
injured. Once that injury clears, the same drill is prophylactic and reads
"Prehab" — even if the athlete is carrying an unrelated injury elsewhere. A
cleared hamstring must not keep reading "Rehab" just because a quad is sore.

The plan's structured blocks carry no injury link (``block_id`` is a slug of the
drill name), so the match is textual. This module resolves each live injury flag
to a canonical body region and expands it into the match terms a client can scan
a block's text for; the per-block decision itself lives in
``web/lib/rehab-label.ts`` so both render surfaces (Plan detail and Today) share
one rule.

Surface (skin) injuries are excluded outright. A blister, graze or covered cut
is a hygiene/friction constraint, not something rehab work targets, so it must
neither pin its own region to "Rehab" nor — when it carries no resolvable
location — drag the whole plan back to "Rehab" by way of ``default_mode``. The
class comes from ``api.contracts.readiness_message.classify_injury_surface`` so
this module never holds a second copy of the rules.

Fail-safe in both directions:
  * a live injury whose region cannot be resolved makes ``default_mode`` "rehab",
    because an unlocalised injury cannot be region-matched and guessing "prehab"
    would understate it;
  * any store failure degrades to a bare "rehab" policy, the pre-existing
    behaviour.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .models import ActiveInjuryRegion, RehabLabelPolicy
from .store import AppStore

LOGGER = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("open", "monitoring")

# Deliberately uncapped. Every bank drill term that survives _bank_drill_terms is
# one whose name contains NO region synonym, so it is the only thing that can
# match that drill — truncating the list would silently downgrade live rehab work
# to "Prehab" for exactly the drills nothing else can catch. The deepest region
# (shoulder) costs ~2.4KB against a payload that already carries the whole
# structured plan, so there is nothing to buy back by trimming.

# LOCATION_MAP resolves a few spellings of one body region to different canonical
# keys ("glute" vs "glutes"). Fold them so an injury and a rehab drill written
# with different spellings still land on the same region.
_REGION_ALIASES = {
    "glutes": "glute",
    "hamstrings": "hamstring",
    "hip flexor": "hip",
    "hip_flexor": "hip",
    "lower_back": "lower back",
    "upper_back": "upper back",
    "si_joint": "si joint",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_PARENTHETICAL = re.compile(r"\([^)]*\)")


def normalize_match_term(value: object) -> str:
    """Lowercase, drop parentheticals, collapse punctuation to single spaces.

    The web client normalizes block text the same way, so a term matches iff the
    normalized term appears as a whole-word run inside the normalized block text.
    """
    text = _PARENTHETICAL.sub(" ", str(value or "").lower())
    return _NON_ALNUM.sub(" ", text).strip()


def _fold_region(value: object) -> str | None:
    region = str(value or "").strip().lower()
    if not region or region == "unspecified":
        return None
    return _REGION_ALIASES.get(region, region)


def _canonical_region(text: str) -> str | None:
    """Resolve free text to a folded canonical body region, or None."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    # Deferred import: keeps the fightcamp NLP/synonym stack out of the import
    # graph of every plan read that never needs it.
    from fightcamp.injury_synonyms import canonicalize_location

    try:
        return _fold_region(canonicalize_location(cleaned))
    except Exception:  # pragma: no cover - never break a plan read on NLP errors
        LOGGER.warning("rehab_label: location canonicalization failed", exc_info=True)
        return None


def _synonym_terms(region: str) -> list[str]:
    from fightcamp.injury_synonyms import LOCATION_MAP

    terms = {region}
    for synonym, canonical in LOCATION_MAP.items():
        if _fold_region(canonical) == region:
            normalized = normalize_match_term(synonym)
            if normalized:
                terms.add(normalized)
    return sorted(terms)


def _bank_drill_terms(region: str, *, covered_by: set[str]) -> list[str]:
    """Rehab-bank drill names for ``region`` that the synonyms do not already catch.

    Bank drills named after their target ("Nordic Hamstring Curl") are redundant
    with the synonym terms. The ones worth shipping are the drills whose name
    never says the body part — "Copenhagen Plank", "Aqua Jogging" — which is
    exactly where plain synonym matching would fail and silently downgrade live
    rehab work to "Prehab".
    """
    from fightcamp.injury_synonyms import LOCATION_MAP
    from fightcamp.rehab_protocols import get_rehab_bank

    try:
        bank = get_rehab_bank()
    except Exception:  # pragma: no cover - bank is packaged data; never fatal
        LOGGER.warning("rehab_label: rehab bank unavailable", exc_info=True)
        return []

    terms: set[str] = set()
    for entry in bank:
        if not isinstance(entry, dict):
            continue
        # Bank locations are controlled taxonomy labels, not athlete free text.
        # Reuse the existing dictionary instead of running NLP for every row.
        location = str(entry.get("location") or "").replace("_", " ")
        if location == "unspecified":
            continue
        bank_region = (
            _fold_region(LOCATION_MAP[location]) if location in LOCATION_MAP
            else _canonical_region(location)
        )
        if bank_region != region:
            continue
        for drill in entry.get("drills") or []:
            name = normalize_match_term(
                drill.get("name") if isinstance(drill, dict) else drill
            )
            if not name or any(f" {term} " in f" {name} " for term in covered_by):
                continue
            terms.add(name)
    return sorted(terms)


def _is_surface_injury(record: dict[str, Any]) -> bool:
    """True when this flag is a skin injury, which rehab labelling ignores.

    Errors resolve to False — a flag we cannot classify keeps its pre-existing
    (cautious) treatment as a real injury rather than being silently dropped.
    """
    # Deferred import: the readiness contract pulls in the injury registry, which
    # no plan read needs unless the athlete actually has live flags.
    from api.contracts.readiness_message import classify_injury_surface

    try:
        return classify_injury_surface(record) != "non_surface"
    except Exception:  # pragma: no cover - never break a plan read on classifier errors
        LOGGER.warning("rehab_label: surface classification failed", exc_info=True)
        return False


def _build_region(region: str) -> ActiveInjuryRegion:
    synonyms = _synonym_terms(region)
    drills = _bank_drill_terms(region, covered_by=set(synonyms))
    return ActiveInjuryRegion(region=region, terms=synonyms + drills)


def resolve_rehab_label_policy(store: AppStore, *, athlete_id: str) -> RehabLabelPolicy:
    """Build the Rehab/Prehab policy from the athlete's live injury flags.

    Source of truth is ``injury_flags`` — NOT the intake "medically cleared"
    answer, which says an athlete may train, not that the injury has resolved.
    Intake injuries are seeded into the flag table and the Today "Cleared" action
    stamps them ``resolved``, so the flags reflect the real current state for
    both origins. Surface (skin) flags are skipped — see the module docstring.
    """
    try:
        flags = store.list_injury_flags(athlete_id, statuses=_ACTIVE_STATUSES, limit=500) or []
    except Exception:  # pragma: no cover - best-effort; never break the plan read
        LOGGER.warning("rehab_label: injury flag read failed", exc_info=True)
        return RehabLabelPolicy()

    regions: dict[str, ActiveInjuryRegion] = {}
    has_unlocalized = False
    for flag in flags:
        record: dict[str, Any] = flag if isinstance(flag, dict) else {}
        if str(record.get("status") or "").strip().lower() not in _ACTIVE_STATUSES:
            continue
        # A skin injury is not rehab work. Skipped before the region resolution
        # below so it can neither claim a region nor, when it has no resolvable
        # location, set has_unlocalized and pin the entire plan to "Rehab".
        if _is_surface_injury(record):
            continue
        body_area = str(record.get("body_area") or "").strip()
        description = str(record.get("description") or "").strip()
        region = _canonical_region(f"{body_area} {description}".strip())
        if not region:
            has_unlocalized = True
            continue
        if region not in regions:
            regions[region] = _build_region(region)

    return RehabLabelPolicy(
        default_mode="rehab" if has_unlocalized else "prehab",
        active_regions=[regions[key] for key in sorted(regions)],
    )
