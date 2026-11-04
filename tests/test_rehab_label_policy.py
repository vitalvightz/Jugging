"""Tests for resolve_rehab_label_policy — the server-side Rehab/Prehab decision.

The policy is driven by the athlete's live injury flags, NOT the intake
"medically cleared" answer, and it is *per body region*: rehab work keeps the
"Rehab" label only while the region it targets is actually injured. These lock
in the behaviours that matter:

  * a cleared hamstring reads "Prehab" even while an unrelated quad is open
    (the bug this replaces: one open flag pinned the whole plan to "Rehab");
  * a still-open injury keeps its own region on "Rehab";
  * an injury whose region cannot be resolved falls back to "rehab" for
    everything, because it cannot be region-matched;
  * a surface (skin) injury is ignored entirely — it is a hygiene constraint,
    not rehab work.
"""
from tests.support import FakeStore

from api.rehab_labels import normalize_match_term, resolve_rehab_label_policy

ATHLETE = "athlete-1"


def _flag(store: FakeStore, *, body_area: str, description: str, status: str) -> None:
    store.create_injury_flag(
        ATHLETE,
        {"body_area": body_area, "description": description, "status": status},
    )


def _policy(store: FakeStore):
    return resolve_rehab_label_policy(store, athlete_id=ATHLETE)


def _matches(policy, block_text: str) -> set[str]:
    """Regions whose terms appear in ``block_text`` — mirrors the web matcher."""
    haystack = f" {normalize_match_term(block_text)} "
    return {
        region.region
        for region in policy.active_regions
        if any(f" {term} " in haystack for term in region.terms)
    }


def test_no_injury_flags_defaults_to_prehab() -> None:
    policy = _policy(FakeStore())
    assert policy.default_mode == "prehab"
    assert policy.active_regions == []


def test_resolved_injury_is_not_active() -> None:
    store = FakeStore()
    _flag(store, body_area="left hamstring", description="strain", status="resolved")
    policy = _policy(store)
    assert policy.default_mode == "prehab"
    assert policy.active_regions == []
    assert _matches(policy, "Isometric Hamstring Bridge Hold") == set()


def test_open_injury_keeps_its_own_region_on_rehab() -> None:
    store = FakeStore()
    _flag(store, body_area="left hamstring", description="strain", status="open")
    policy = _policy(store)
    assert [region.region for region in policy.active_regions] == ["hamstring"]
    assert _matches(policy, "Isometric Hamstring Bridge Hold") == {"hamstring"}


def test_monitoring_injury_counts_as_active() -> None:
    store = FakeStore()
    _flag(store, body_area="left knee", description="tendon pain", status="monitoring")
    policy = _policy(store)
    assert [region.region for region in policy.active_regions] == ["knee"]


def test_cleared_region_reads_prehab_while_another_region_is_open() -> None:
    # The regression this replaces: a cleared hamstring stayed on "Rehab" purely
    # because an unrelated quad was open. Each region now answers for itself.
    store = FakeStore()
    _flag(store, body_area="left hamstring", description="strain", status="resolved")
    _flag(store, body_area="left quad", description="tightness", status="open")
    policy = _policy(store)
    assert policy.default_mode == "prehab"
    assert [region.region for region in policy.active_regions] == ["quads"]
    assert _matches(policy, "Isometric Hamstring Bridge Hold") == set()
    assert _matches(policy, "Spanish Squat Isometric — quad focus") == {"quads"}


def test_bank_drill_names_cover_blocks_that_never_name_the_region() -> None:
    # "Copenhagen" says nothing about the groin, so synonyms alone would let live
    # groin rehab read as "Prehab". The rehab bank supplies the missing term.
    store = FakeStore()
    _flag(store, body_area="groin", description="strain", status="open")
    policy = _policy(store)
    groin = next(region for region in policy.active_regions if region.region == "groin")
    assert any("copenhagen" in term for term in groin.terms)


def test_deep_bank_regions_ship_every_drill_term() -> None:
    # Regression: the term list was capped at 60 per region. Every term that
    # survives _bank_drill_terms is a drill whose name contains NO region
    # synonym, so it is the only thing that can match that drill — truncating
    # dropped exactly the terms nothing else could catch, downgrading live rehab
    # work to "Prehab". "Suitcase Carry" is a real lower-back bank drill that sat
    # at index 60, the first casualty of the cap.
    store = FakeStore()
    _flag(store, body_area="lower back", description="strain", status="open")
    policy = _policy(store)
    lower_back = next(
        region for region in policy.active_regions if region.region == "lower back"
    )

    assert len(lower_back.terms) > 60, "expected a region deep enough to exercise the old cap"
    assert "suitcase carry" in lower_back.terms
    assert lower_back.terms.index("suitcase carry") >= 60
    assert _matches(policy, "Suitcase Carry") == {"lower back"}


def test_no_active_region_term_is_dropped_by_a_cap() -> None:
    # Guards the cap against returning for ANY region, not just the one above:
    # every term past the old 60-item cutoff must still match its own block.
    store = FakeStore()
    for body_area in ("shoulder", "wrist", "hand", "lower back"):
        _flag(store, body_area=body_area, description="strain", status="open")
    policy = _policy(store)

    deep = [region for region in policy.active_regions if len(region.terms) > 60]
    assert deep, "expected at least one region deeper than the old cap"
    for region in deep:
        for term in region.terms[60:]:
            assert region.region in _matches(policy, term), (
                f"{region.region} term {term!r} past the old cap no longer matches"
            )


def test_unlocalizable_open_injury_falls_back_to_rehab() -> None:
    # Nothing in the flag resolves to a body region, so no block can be matched
    # against it. Everything stays "Rehab" rather than guessing "Prehab".
    store = FakeStore()
    _flag(store, body_area="", description="feels off after sparring", status="open")
    policy = _policy(store)
    assert policy.default_mode == "rehab"
    assert policy.active_regions == []


def test_unlocalizable_open_injury_does_not_hide_a_localized_one() -> None:
    store = FakeStore()
    _flag(store, body_area="", description="feels off after sparring", status="open")
    _flag(store, body_area="left quad", description="tightness", status="open")
    policy = _policy(store)
    assert policy.default_mode == "rehab"
    assert [region.region for region in policy.active_regions] == ["quads"]


def test_localized_skin_injury_does_not_claim_its_region() -> None:
    # Regression: surface injuries started landing in injury_flags, and a graze
    # on the ribs pinned the whole chest region to "Rehab". A skin wound is a
    # hygiene constraint — there is no rehab work for it to keep named.
    store = FakeStore()
    store.create_injury_flag(
        ATHLETE,
        {
            "body_area": "ribs",
            "description": "graze",
            "skin_integrity": "open",
            "coverable": "yes",
            "status": "open",
        },
    )
    policy = _policy(store)
    assert policy.default_mode == "prehab"
    assert policy.active_regions == []


def test_unlocalized_skin_injury_does_not_pin_the_plan_to_rehab() -> None:
    # The regression that broke Rehab->Prehab outright: a skin injury with no
    # resolvable body area counted as "unlocalizable", flipping default_mode to
    # "rehab" and dragging EVERY cleared region back to "Rehab" plan-wide.
    store = FakeStore()
    _flag(store, body_area="left hamstring", description="strain", status="resolved")
    store.create_injury_flag(
        ATHLETE,
        {
            "body_area": "",
            "description": "mat burn on my side",
            "skin_integrity": "intact",
            "coverable": "yes",
            "status": "open",
        },
    )
    policy = _policy(store)
    assert policy.default_mode == "prehab"
    assert policy.active_regions == []
    assert _matches(policy, "Isometric Hamstring Bridge Hold") == set()


def test_skin_injury_does_not_hide_a_real_injury_coming_back() -> None:
    # The other half of the fix: skin is ignored, but a genuine (non-surface)
    # injury returning on a location puts that region back on "Rehab".
    store = FakeStore()
    store.create_injury_flag(
        ATHLETE,
        {
            "body_area": "ribs",
            "description": "graze",
            "skin_integrity": "intact",
            "coverable": "yes",
            "status": "open",
        },
    )
    _flag(store, body_area="left hamstring", description="strain came back", status="open")
    policy = _policy(store)
    assert policy.default_mode == "prehab"
    assert [region.region for region in policy.active_regions] == ["hamstring"]
    assert _matches(policy, "Isometric Hamstring Bridge Hold") == {"hamstring"}


def test_region_spellings_fold_to_one_entry() -> None:
    # "glute" and "glutes" are distinct canonical keys upstream; two flags that
    # name the same region must not produce two regions.
    store = FakeStore()
    _flag(store, body_area="glute", description="strain", status="open")
    _flag(store, body_area="buttocks", description="soreness", status="open")
    policy = _policy(store)
    assert [region.region for region in policy.active_regions] == ["glute"]


def test_store_without_injury_flags_support_defaults_to_rehab() -> None:
    class BareStore:
        pass

    policy = resolve_rehab_label_policy(BareStore(), athlete_id=ATHLETE)
    assert policy.default_mode == "rehab"
    assert policy.active_regions == []


def test_store_failure_defaults_to_rehab() -> None:
    class BrokenStore:
        def list_injury_flags(self, *args, **kwargs):
            raise RuntimeError("supabase down")

    policy = resolve_rehab_label_policy(BrokenStore(), athlete_id=ATHLETE)
    assert policy.default_mode == "rehab"
    assert policy.active_regions == []


def test_packaged_bank_locations_match_the_free_text_resolver() -> None:
    """The fast taxonomy lookup preserves the old NLP result for every label."""
    from fightcamp.injury_synonyms import LOCATION_MAP
    from fightcamp.rehab_protocols import get_rehab_bank
    from api.rehab_labels import _canonical_region, _fold_region

    locations = {str(entry.get("location") or "").replace("_", " ") for entry in get_rehab_bank()}
    for location in locations:
        if location == "unspecified":
            assert _canonical_region(location) is None
            continue
        assert location in LOCATION_MAP, location
        assert _fold_region(LOCATION_MAP[location]) == _canonical_region(location), location


def test_bank_labels_skip_nlp_but_unknown_labels_keep_the_fallback(monkeypatch) -> None:
    from api import rehab_labels
    from fightcamp import rehab_protocols

    calls = []
    def canonicalize(text):
        calls.append(text)
        return "hamstring"

    monkeypatch.setattr(rehab_labels, "_canonical_region", canonicalize)
    monkeypatch.setattr(rehab_protocols, "get_rehab_bank", lambda: [
        {"location": "hamstrings", "drills": [{"name": "Nordic Curl"}]},
        {"location": "future taxonomy label", "drills": [{"name": "New Curl"}]},
        {"location": "unspecified", "drills": [{"name": "Unlocalized Drill"}]},
    ])
    assert rehab_labels._bank_drill_terms("hamstring", covered_by=set()) == ["new curl", "nordic curl"]
    assert calls == ["future taxonomy label"]
