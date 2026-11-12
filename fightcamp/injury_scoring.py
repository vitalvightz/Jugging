from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, List, TypedDict

from .injury_negation import remove_negated_phrases
from .injury_synonyms import (
    EXCLUSIVE_HINTS,
    IMPINGEMENT_GATE_HINTS,
    IMPINGEMENT_LOW_SPECIFICITY,
    INJURY_SYNONYM_MAP,
    LOCATION_MAP,
    SORENESS_HINTS,
    STIFFNESS_HINTS,
    TENDONITIS_REQUIRED_HINTS,
    TIGHTNESS_HINTS,
    TYPE_PRIORITY,
    detect_triage_category,
    detect_structural_red_flags,
)

# -----------------------------
# 1) CANONICAL ALIGNMENT
# -----------------------------
# Only these injury_type keys exist in your rehab protocol database
CANONICAL_TYPES: List[str] = list(INJURY_SYNONYM_MAP.keys())

# Map medical terms to (canonical_type, flag)
MEDICAL_MAP: Dict[str, tuple[str, str]] = {
    "fracture": ("fracture", "urgent_fracture"),
    "fractured": ("fracture", "urgent_fracture"),
    "dislocation": ("dislocation", "urgent_dislocation"),
    "dislocated": ("dislocation", "urgent_dislocation"),
    "infection": ("infection", "urgent_infection"),
    "infected": ("infection", "urgent_infection"),
    "nerve": ("nerve_involvement", "urgent_nerve"),
    "hernia": ("hernia", "urgent_hernia"),
    "bursitis": ("tendonitis", "bursitis_variant"),
    "shin splints": ("pain", "shin_splints_variant"),
}

# Urgent terms should trigger a clear escalation flag without breaking rehab lookup.
URGENT_TERMS = {"fracture", "fractured", "dislocation", "dislocated", "infection", "infected", "nerve", "hernia"}

# Optional: lightweight mechanical red-flags (no NegEx here; assume pre-cleaned text)
RED_FLAG_TERMS: Dict[str, str] = {
    "locking": "mechanical_locking",
    "giving way": "instability_event",
    "buckled": "instability_event",
    "numb": "nerve_involvement",
    "tingling": "nerve_involvement",
}


class ScoredInjuryPhrase(TypedDict):
    injury_type: str
    rehab_type: str
    triage_category: str
    location: str
    side: str
    flags: list[str]
    raw_text: str


def _normalize(text: str) -> str:
    """Lowercase + compress whitespace. Keep hyphens (for l-spine) and slashes (for n/a)."""
    return " ".join((text or "").lower().strip().split())


@lru_cache(maxsize=None)
def _phrase_pattern(p: str) -> re.Pattern[str]:
    return re.compile(rf"(?:^|\W){re.escape(p)}(?:\W|$)")


def safe_phrase_search(phrase: str, text: str) -> bool:
    """
    Boundary match that works for:
    - single words
    - multi-word phrases
    - hyphenated tokens (e.g., l-spine) when phrase includes hyphen
    """
    t = _normalize(text)
    p = _normalize(phrase)
    if not p or not t:
        return False
    return _phrase_pattern(p).search(t) is not None


def _build_location_map(location_map: dict[str, str]) -> dict[str, list[str]]:
    canonical_map: dict[str, list[str]] = {}
    for synonym, canonical in location_map.items():
        if canonical not in canonical_map:
            canonical_map[canonical] = []
        if synonym not in canonical_map[canonical]:
            canonical_map[canonical].append(synonym)
    return canonical_map


LOCATION_MAP: dict[str, list[str]] = _build_location_map(LOCATION_MAP)


def _first_location_hit(t_clean: str) -> str:
    """
    Returns first matched canonical location.
    Deterministic: iterates LOCATION_MAP in insertion order, but a generic
    word that maps to "unspecified" (e.g. "arm") never shadows a specific
    region in the same text (e.g. "back of arm" -> triceps).
    """
    for loc, syns in LOCATION_MAP.items():
        if loc == "unspecified":
            continue
        for s in syns:
            if safe_phrase_search(s, t_clean):
                return loc
    return "unspecified"


def _detect_side(t_clean: str) -> str:
    """
    Safe side detection:
    - default unspecified (not 'both')
    - no single-letter hacks
    """
    if any(safe_phrase_search(p, t_clean) for p in ["both", "bilateral"]):
        return "both"
    if safe_phrase_search("left", t_clean):
        return "left"
    if safe_phrase_search("right", t_clean):
        return "right"
    return "unspecified"


def score_injury_phrase(t_clean: str, synonym_map: Dict[str, List[str]] | None = None) -> ScoredInjuryPhrase:
    """
    Processes ALREADY CLEANED text (post-negation, post-split).
    - Does NOT do NegEx here (assume upstream handles negation).
    - Keeps output schema stable for downstream rehab lookup.

    Expected `synonym_map` format:
      { canonical_type: ["syn1", "syn2", ...], ... }
    """
    t_clean = _normalize(remove_negated_phrases(t_clean or ""))
    if synonym_map is None:
        # The default maps are module constants, so the score is a pure
        # function of the negation-stripped text. Today scores the same few
        # injury phrases dozens of times per build; each caller still gets its
        # own result and its own ``flags`` list.
        cached = _score_default_maps(t_clean)
        return {**cached, "flags": list(cached["flags"])}  # type: ignore[typeddict-item]
    return _score_cleaned_phrase(t_clean, synonym_map)


@lru_cache(maxsize=4096)
def _score_default_maps(t_clean: str) -> ScoredInjuryPhrase:
    return _score_cleaned_phrase(t_clean, None)


def _score_cleaned_phrase(t_clean: str, synonym_map: Dict[str, List[str]] | None) -> ScoredInjuryPhrase:
    if not t_clean:
        return {
            "injury_type": "unspecified",
            "rehab_type": "unspecified",
            "triage_category": "",
            "location": "unspecified",
            "side": "unspecified",
            "flags": [],
            "raw_text": "",
        }

    # A) Defaults (schema-stable)
    side = _detect_side(t_clean)
    injury_type = "unspecified"
    rehab_type = "unspecified"
    triage_category = ""
    location = "unspecified"
    flags: List[str] = []
    medical_hit = False
    structural_hit = False

    triage_category = detect_triage_category(t_clean)
    structural_flags = detect_structural_red_flags(t_clean)
    if structural_flags:
        structural_hit = True
        flags.extend(structural_flags)

    # B) Medical terms first (can set canonical type + flags)
    for term, (canon, flag) in MEDICAL_MAP.items():
        if safe_phrase_search(term, t_clean):
            medical_hit = True
            if injury_type == "unspecified":
                injury_type = canon
            flags.append(flag)
            if term in URGENT_TERMS:
                flags.append("urgent")

    # C) Red flags (independent of type)
    for term, flag in RED_FLAG_TERMS.items():
        if safe_phrase_search(term, t_clean):
            flags.append(flag)

    # D) Canonical type scoring (cap at 1.5 per category; no stacking)
    # If medical term already matched, we STILL compute scores, but we only override
    # if injury_type is unspecified (prevents "fracture" being replaced by "strain").
    type_scores: Dict[str, float] = {k: 0.0 for k in CANONICAL_TYPES}

    for cat, syns in (synonym_map or INJURY_SYNONYM_MAP).items():
        if cat not in CANONICAL_TYPES:
            continue

        # Match on canonical label OR any synonym (first hit wins for that category)
        for s in [cat] + list(syns or []):
            if safe_phrase_search(s, t_clean):
                type_scores[cat] = 1.5
                break

    def _has_any_hint(hints: set[str]) -> bool:
        return any(safe_phrase_search(hint, t_clean) for hint in hints)

    # Deterministic hint boosts/gates for ambiguous overlaps.
    if _has_any_hint(EXCLUSIVE_HINTS.get("instability", set())):
        type_scores["instability"] = max(type_scores.get("instability", 0.0), 1.6)
    if _has_any_hint(EXCLUSIVE_HINTS.get("sprain", set())):
        type_scores["sprain"] = max(type_scores.get("sprain", 0.0), 1.55)

    if _has_any_hint(SORENESS_HINTS):
        type_scores["soreness"] = max(type_scores.get("soreness", 0.0), 1.6)
    if _has_any_hint(STIFFNESS_HINTS):
        type_scores["stiffness"] = max(type_scores.get("stiffness", 0.0), 1.6)
    if _has_any_hint(TIGHTNESS_HINTS):
        type_scores["tightness"] = max(type_scores.get("tightness", 0.0), 1.6)

    # Tendonitis should only beat generic pain when tendon/overuse context exists.
    if type_scores.get("tendonitis", 0.0) > 0 and not _has_any_hint(TENDONITIS_REQUIRED_HINTS):
        type_scores["tendonitis"] = 0.0

    # Clicking/catching alone is low-specificity for impingement.
    low_specificity_impingement = _has_any_hint(IMPINGEMENT_LOW_SPECIFICITY)
    has_gate_impingement = _has_any_hint(IMPINGEMENT_GATE_HINTS)
    if type_scores.get("impingement", 0.0) > 0 and low_specificity_impingement and not has_gate_impingement:
        type_scores["impingement"] = 0.0
    elif has_gate_impingement:
        type_scores["impingement"] = max(type_scores.get("impingement", 0.0), 1.6)

    # If we have no medical type set (or it's still unspecified), use best score
    if triage_category:
        rehab_type = "unspecified"
    elif injury_type == "unspecified" and any(type_scores.values()) and not medical_hit and not structural_hit:
        scored_candidates = [
            (cat, score)
            for cat, score in type_scores.items()
            if score > 0 and cat in CANONICAL_TYPES
        ]
        scored_candidates.sort(
            key=lambda item: (
                item[1],
                TYPE_PRIORITY.get(item[0], 0.0),
                -CANONICAL_TYPES.index(item[0]),
            ),
            reverse=True,
        )
        injury_type = scored_candidates[0][0]
        rehab_type = injury_type
    else:
        rehab_type = injury_type

    if rehab_type != "unspecified" and injury_type not in CANONICAL_TYPES:
        rehab_type = "unspecified"

    # Severe structural injuries are routed by triage_category + flags, never by
    # a soft rehab type. Mirror parse_injury_phrase: structural red flags force
    # both injury and rehab type to "unspecified" so dislocations, ruptures and
    # fractures never land in ordinary rehab buckets.
    if structural_hit:
        injury_type = "unspecified"
        rehab_type = "unspecified"

    # E) Location detection (deterministic)
    location = _first_location_hit(t_clean)

    # F) Extra defensive: if medical term hit but location is missing, keep unspecified
    # (do not invent location)
    # If you want, you can add additional med-term-to-location heuristics later.

    return {
        "injury_type": injury_type,
        "rehab_type": rehab_type,
        "triage_category": triage_category,
        "location": location,
        "side": side,
        "flags": sorted(set(flags)),
        "raw_text": t_clean,
    }
