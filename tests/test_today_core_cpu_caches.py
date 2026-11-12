"""Repeated pure work in a Today build is done once, with identical answers.

A Today build used to run spaCy ~200 times over ~27 distinct injury phrases,
re-score the same phrases, and re-read and re-validate the clinical policy
files several times. These caches only remove repeats: each test pins that the
cached answer equals a fresh computation and cannot be changed by a caller.
"""

from __future__ import annotations

import os
import shutil

import pytest

from api import server_timing
from fightcamp import injury_negation, injury_scoring, injury_synonyms, rehab_clinical


PHRASES = ("knee pain", "lower back lower back pain", "Left ankle sprain", "no shoulder pain",
           "denies knee swelling", "torn ACL", "right hamstring strain, no bruising", "")


def test_negex_filtering_is_computed_once_per_text_with_the_same_answer():
    nlp = injury_synonyms.get_nlp()
    if not (nlp and injury_synonyms._NEGSPACY_AVAILABLE):
        pytest.skip("spaCy negation pipeline not installed")
    injury_negation._remove_negex_phrases.cache_clear()
    first = [injury_negation.remove_negated_phrases(text) for text in PHRASES]
    misses = injury_negation._remove_negex_phrases.cache_info().misses
    again = [injury_negation.remove_negated_phrases(text) for text in PHRASES]

    assert again == first
    assert injury_negation._remove_negex_phrases.cache_info().misses == misses
    # Same answer as running the pipeline directly, uncached.
    for text, cached in zip(PHRASES, first):
        if text:
            assert cached == injury_negation._remove_negex_phrases.__wrapped__(nlp, text)


def test_without_spacy_the_fallback_is_used_not_a_cached_spacy_answer(monkeypatch):
    with_spacy = injury_negation.remove_negated_phrases("no shoulder pain and knee pain")
    monkeypatch.setattr(injury_synonyms, "get_nlp", lambda: None)
    fallback = injury_negation.remove_negated_phrases("no shoulder pain and knee pain")
    assert fallback == injury_negation._strip_negated_chunks_fallback("no shoulder pain and knee pain")
    assert isinstance(with_spacy, str)


@pytest.mark.parametrize("text", PHRASES)
def test_cached_phrase_scores_match_a_fresh_score(text):
    cleaned = injury_scoring._normalize(injury_negation.remove_negated_phrases(text))
    fresh = injury_scoring._score_cleaned_phrase(cleaned, None)
    assert injury_scoring.score_injury_phrase(text) == fresh
    assert injury_scoring.score_injury_phrase(text) == fresh


def test_a_caller_cannot_change_the_next_callers_score():
    first = injury_scoring.score_injury_phrase("torn ACL with swelling")
    first["flags"].append("tampered")
    first["injury_type"] = "tampered"

    second = injury_scoring.score_injury_phrase("torn ACL with swelling")
    assert "tampered" not in second["flags"]
    assert second["injury_type"] != "tampered"


def test_an_explicit_synonym_map_is_never_served_from_the_default_cache():
    custom_map = {"sprain": ["pain"]}
    injury_scoring.score_injury_phrase("knee pain")  # warm the default-map cache
    custom = injury_scoring.score_injury_phrase("knee pain", custom_map)
    assert custom == injury_scoring._score_cleaned_phrase("knee pain", custom_map)
    assert custom["injury_type"] == "sprain"


def test_default_policies_are_composed_once_while_the_files_are_unchanged():
    rehab_clinical._DEFAULT_POLICIES = None
    first = rehab_clinical.load_clinical_policies()
    assert rehab_clinical.load_clinical_policies() is first
    assert first == rehab_clinical._compose_clinical_policies(None)


def test_a_changed_policy_file_is_composed_again(tmp_path, monkeypatch):
    copy = tmp_path / "rehab_pathways.json"
    shutil.copy(rehab_clinical.PATHWAYS_PATH, copy)
    monkeypatch.setattr(rehab_clinical, "PATHWAYS_PATH", copy)
    rehab_clinical._DEFAULT_POLICIES = None
    first = rehab_clinical.load_clinical_policies()

    stat = copy.stat()
    os.utime(copy, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    second = rehab_clinical.load_clinical_policies()

    assert second is not first
    assert second == first
    rehab_clinical._DEFAULT_POLICIES = None


def test_explicit_path_or_bank_always_composes_fresh():
    rehab_clinical._DEFAULT_POLICIES = None
    cached = rehab_clinical.load_clinical_policies()
    explicit = rehab_clinical.load_clinical_policies(rehab_clinical.PATHWAYS_PATH)
    assert explicit is not cached
    assert explicit == cached


@pytest.mark.parametrize("header, now_ms, expected", [
    ("t=1700000000000", 1700000000125.0, 125.0),
    (" t=1700000000000 ", 1700000000000.0, 0.0),
    (None, 1700000000125.0, None),
    ("", 1700000000125.0, None),
    ("t=abc", 1700000000125.0, None),
    ("1700000000000", 1700000000125.0, None),
    ("t=1700000000000", 1699999999000.0, None),  # stamp in the future: skewed
    ("t=1600000000000", 1700000000000.0, None),  # implausibly old: forged
])
def test_proxy_queue_time_is_parsed_and_bounded(header, now_ms, expected):
    assert server_timing.proxy_queue_ms(header, now_ms) == expected


def test_responses_report_proxy_queue_time_when_the_proxy_stamps_the_request():
    import time

    from support import _build_client

    client, _, _ = _build_client()
    stamped = client.get("/health", headers={"X-Request-Start": f"t={int(time.time() * 1000) - 40}"})
    unstamped = client.get("/health")

    entries = [part.split(";")[0] for part in stamped.headers["server-timing"].split(", ")]
    assert entries[0] == "queue" and entries[-1] == "app"
    queued = float(stamped.headers["server-timing"].split(", ")[0].split("dur=")[1])
    assert 40 <= queued < 5_000
    assert unstamped.headers["server-timing"].startswith("app;dur=")
