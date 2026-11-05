import sys
from pathlib import Path
import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from fightcamp.injury_filtering import normalize_injury_regions
from fightcamp.injury_formatting import parse_injury_entry, parse_injuries_and_restrictions
import fightcamp.injury_synonyms as injury_synonyms
from fightcamp.injury_negation import (
    contains_negated_injury,
    negation_detection_available,
    remove_negated_phrases,
)
from fightcamp.injury_synonyms import (
    canonicalize_injury_type,
    detect_structural_red_flags,
    parse_injury_phrase,
    split_injury_text,
)
from fightcamp.injury_scoring import score_injury_phrase
from fightcamp.injury_danger_terms import detect_danger_term_routes
from fightcamp.rehab_protocols import generate_rehab_protocols


def test_negation_strips_injury_phrases():
    samples = [
        "no shoulder pain",
        "not injured",
        "no shin splints",
        "never had knee issues",
    ]
    for sample in samples:
        assert parse_injury_entry(sample) is None
        assert normalize_injury_regions([sample]) == set()


def test_multi_injury_split_and_parse():
    sample = "Left ankle sprain / right wrist pain; shoulder tightness, knee soreness."
    phrases = split_injury_text(sample)
    assert len(phrases) == 4
    entries = [parse_injury_entry(phrase) for phrase in phrases]
    entries = [entry for entry in entries if entry]
    locations = {entry.get("canonical_location") for entry in entries}
    assert {"ankle", "wrist", "shoulder", "knee"} <= locations


def test_raw_knee_mechanism_phrase_stays_single_injury_entry():
    sample = "Right knee went back as I planted it I think I overextended it"
    injuries, restrictions = parse_injuries_and_restrictions(sample)
    assert restrictions == []
    assert len(injuries) == 1
    assert injuries[0].get("canonical_location") == "knee"
    locations = {str(entry.get("canonical_location") or "").lower() for entry in injuries}
    assert "lower back" not in locations
    assert "lower_back" not in locations


def test_raw_knee_phrase_with_and_stays_single_injury_entry():
    sample = "Right knee went back and gave way on plant"
    injuries, restrictions = parse_injuries_and_restrictions(sample)
    assert restrictions == []
    assert len(injuries) == 1
    assert injuries[0].get("canonical_location") == "knee"
    locations = {str(entry.get("canonical_location") or "").lower() for entry in injuries}
    assert "lower back" not in locations
    assert "lower_back" not in locations


def test_raw_knee_planted_and_twisted_stays_single_injury_entry():
    sample = "Right knee planted and twisted"
    injuries, restrictions = parse_injuries_and_restrictions(sample)
    assert restrictions == []
    assert len(injuries) == 1
    assert injuries[0].get("canonical_location") == "knee"


def test_raw_legacy_multi_injury_with_semicolon_still_splits():
    sample = "right knee sprain; left ankle soreness"
    injuries, restrictions = parse_injuries_and_restrictions(sample)
    assert restrictions == []
    assert len(injuries) == 2
    assert {entry.get("canonical_location") for entry in injuries} == {"knee", "ankle"}


def test_raw_legacy_multi_injury_with_comma_still_splits():
    sample = "right knee sprain, left ankle soreness"
    injuries, restrictions = parse_injuries_and_restrictions(sample)
    assert restrictions == []
    assert len(injuries) == 2
    assert {entry.get("canonical_location") for entry in injuries} == {"knee", "ankle"}


def test_and_split_still_works_for_distinct_body_parts():
    sample = "right wrist pain and shoulder tightness"
    injuries, restrictions = parse_injuries_and_restrictions(sample)
    assert restrictions == []
    assert len(injuries) == 2
    assert {entry.get("canonical_location") for entry in injuries} == {"wrist", "shoulder"}


def test_and_split_still_works_for_ankle_and_wrist():
    sample = "ankle sprain and wrist pain"
    injuries, restrictions = parse_injuries_and_restrictions(sample)
    assert restrictions == []
    assert len(injuries) == 2
    assert {entry.get("canonical_location") for entry in injuries} == {"ankle", "wrist"}


def test_punctuation_and_hyphenated_phrases_parse():
    sample = "Shoulder. left-ankle sprain (mild); knee pain."
    phrases = split_injury_text(sample)
    entries = [parse_injury_phrase(phrase) for phrase in phrases]
    locations = {loc for _, loc in entries if loc}
    assert {"shoulder", "ankle", "knee"} <= locations


def test_negated_phrase_fallback_normalizes_dash_variants(monkeypatch):
    monkeypatch.setattr(injury_synonyms, "get_nlp", lambda: None)

    assert remove_negated_phrases("no shoulder pain — knee soreness") == "knee soreness"
    assert (
        remove_negated_phrases(
            f"no shoulder pain {chr(0x00E2)}{chr(0x20AC)}{chr(0x201D)} knee soreness"
        )
        == "knee soreness"
    )


def test_negation_regressions_preserve_behaviour():
    assert remove_negated_phrases("no shoulder pain - knee soreness") == "knee soreness"
    assert remove_negated_phrases("no shoulder pain — knee soreness") == "knee soreness"
    assert remove_negated_phrases("no shoulder pain â€” knee soreness") == "knee soreness"
    assert "fracture" not in remove_negated_phrases("doctor ruled out fracture but ankle hurts")

    assert parse_injury_entry("no shin splints") is None

    ruled_out_fracture = score_injury_phrase(remove_negated_phrases("doctor ruled out fracture but ankle hurts"))
    assert "urgent_fracture" not in ruled_out_fracture["flags"]

    no_nerve = score_injury_phrase(remove_negated_phrases("no numbness or tingling"))
    assert "urgent_nerve" not in no_nerve["flags"]
    assert "nerve_involvement" not in no_nerve["flags"]

    no_concussion = score_injury_phrase(remove_negated_phrases("no concussion symptoms, just tired"))
    assert "suspected_concussion" not in no_concussion["flags"]
    assert "urgent" not in no_concussion["flags"]



@pytest.mark.parametrize(
    "phrase",
    [
        "no shoulder pain",
        "no shin splints",
        "never had knee issues",
        "denies knee pain",
        "not injured",
    ],
)
def test_strong_negation_cues_strip_injury(phrase):
    # Strong cues (no/never/denies/denied/deny) and the adjacent soft-cue form
    # "not injured" negate the whole clause: they are flagged and removed.
    assert contains_negated_injury(phrase) is True
    assert remove_negated_phrases(phrase) == ""
    assert parse_injury_entry(phrase) is None


@pytest.mark.parametrize(
    ("phrase", "preserved"),
    [
        # Uncertainty, severity and progression qualifiers — "not"/"without" here
        # describe the qualifier, not an absent injury, so the symptom survives.
        ("not sure if my knee pain is from football", "knee pain"),
        ("not severe ankle swelling", "ankle swelling"),
        ("not improving Achilles pain", "achilles pain"),
        # Timing and equipment — the Achilles/knee injury is real and stays.
        ("without warning my Achilles popped", "achilles"),
        ("without brace knee pain continues", "knee pain"),
        # Trailing soft cue (equipment) never strips the leading injury.
        ("knee pain without brace", "knee pain"),
    ],
)
def test_soft_negation_cues_preserve_real_injuries(phrase, preserved):
    # "not"/"without" must not be treated as injury negation just because they
    # lead the phrase: they are not flagged, and the injury text is preserved.
    assert contains_negated_injury(phrase) is False
    assert preserved in remove_negated_phrases(phrase).lower()


@pytest.mark.parametrize(
    ("phrase", "preserved"),
    [
        ("no shoulder pain but knee swelling", "knee swelling"),
        ("denies shin splints, reports Achilles pain", "achilles pain"),
        ("shoulder clicking no pain", "shoulder clicking"),
        ("knee pain without brace", "knee pain"),
    ],
)
def test_negation_keeps_non_negated_injury_half(phrase, preserved):
    assert preserved in remove_negated_phrases(phrase).lower()


def test_parse_injury_entry_checks_negation_availability_at_parse_time(monkeypatch):
    monkeypatch.setattr(injury_synonyms, "get_nlp", lambda: None)

    assert not negation_detection_available()

    entry = parse_injury_entry("no shoulder pain - knee soreness")

    assert entry is not None
    assert entry["canonical_location"] == "knee"
    assert entry["injury_type"] == "soreness"


def test_canonical_injury_types_are_allowed():
    allowed = {
        "sprain",
        "strain",
        "tightness",
        "contusion",
        "swelling",
        "tendonitis",
        "impingement",
        "instability",
        "stiffness",
        "pain",
        "soreness",
        "hyperextension",
        "abrasion",
        "cut",
        "laceration",
        "graze",
        "blister",
        "unspecified",
    }
    entry = parse_injury_entry("shin splints")
    assert entry is not None
    assert entry.get("injury_type") in allowed
    assert entry.get("injury_type") != "shin splints"


def test_rehab_lookup_fallbacks_for_location_aliases():
    text, _ = generate_rehab_protocols(
        injury_string="lower back pain, biceps strain",
        exercise_data=[],
        current_phase="GPP",
    )
    assert "No rehab options" not in text


def test_surface_injury_types_parse_as_first_class_types():
    cases = [
        ("neck abrasion", "abrasion", "neck"),
        ("neck cut", "cut", "neck"),
        ("neck laceration", "laceration", "neck"),
        ("neck graze", "graze", "neck"),
        ("foot blister", "blister", "foot"),
        ("scraped knee", "abrasion", "knee"),
        ("grazed elbow", "graze", "elbow"),
        ("deep gash on cheek", "laceration", "face"),
        ("boot rub on heel", "blister", "heel"),
    ]
    for phrase, expected_type, expected_location in cases:
        parsed_type, parsed_location = parse_injury_phrase(phrase)
        assert parsed_type == expected_type
        assert parsed_location == expected_location


def test_cut_above_eye_parses_to_cut_type_and_eye_adjacent_location():
    parsed_type, parsed_location = parse_injury_phrase("cut above eye")
    assert parsed_type == "cut"
    assert parsed_location in {"eye", "face"}


def test_surface_terms_do_not_fall_back_to_old_pain_or_contusion_buckets():
    assert parse_injury_phrase("neck abrasion")[0] != "contusion"
    assert parse_injury_phrase("grazed elbow")[0] != "pain"
    assert parse_injury_phrase("neck cut")[0] != "strain"
    assert parse_injury_phrase("neck graze")[0] != "strain"
    assert parse_injury_phrase("neck abrasion")[0] != "strain"

    parsed_type, _ = parse_injury_phrase("surface scrape on knee")
    assert parsed_type == "abrasion"
    assert parsed_type != "pain"


def test_injury_synonym_map_has_no_duplicate_synonyms():
    seen = {}
    for category, synonyms in injury_synonyms.INJURY_SYNONYM_MAP.items():
        for synonym in synonyms:
            seen.setdefault(synonym, []).append(category)

    duplicates = {synonym: categories for synonym, categories in seen.items() if len(categories) > 1}

    assert duplicates == {}


def test_overbroad_injury_synonyms_do_not_create_one_word_false_positives(monkeypatch):
    monkeypatch.setattr(injury_synonyms, "get_nlp", lambda: None)

    assert parse_injury_phrase("blood flow only") == (None, None)
    assert parse_injury_phrase("hot day") == (None, None)
    assert parse_injury_phrase("elbow soreness") == ("soreness", "elbow")
    assert parse_injury_phrase("left arm pain") == ("pain", "unspecified")
    assert parse_injury_phrase("spine pain") == ("pain", "unspecified")
    assert parse_injury_phrase("grade 2 calf strain") == ("strain", "calf")


def test_location_map_uses_safe_broad_regions_for_ambiguous_anatomy(monkeypatch):
    monkeypatch.setattr(injury_synonyms, "get_nlp", lambda: None)

    cases = [
        ("arm pain", "unspecified"),
        ("upper arm pain", "unspecified"),
        ("lower arm pain", "forearm"),
        ("fibula pain", "shin"),
        ("outer calf bone pain", "shin"),
        ("collarbone pain", "chest"),
        ("clavicle pain", "chest"),
        ("spine pain", "unspecified"),
        ("lower spine pain", "lower back"),
        ("lumbar pain", "lower back"),
        ("l-spine pain", "lower back"),
        ("cervical spine pain", "neck"),
        ("c-spine pain", "neck"),
        ("thoracic spine pain", "upper back"),
        ("t-spine pain", "upper back"),
        ("femur pain", "unspecified"),
        ("cheek cut", "face"),
        ("facial cheek cut", "face"),
        ("face cheek cut", "face"),
        ("butt cheeks pain", "glutes"),
        ("glute cheek pain", "glutes"),
        ("glute cheeks pain", "glutes"),
        ("cheeks pain", "unspecified"),
        ("jawbone pain", "jaw"),
    ]
    for phrase, expected_location in cases:
        _, location = parse_injury_phrase(phrase)
        assert location == expected_location


def test_generate_rehab_protocols_structured_urgent_entry_blocks_normal_rehab():
    text, _ = generate_rehab_protocols(
        injury_string="right knee acl tear",
        exercise_data=[],
        current_phase="GPP",
        parsed_entries=[
            {
                "canonical_location": "knee",
                "injury_type": "unspecified",
                "rehab_type": "unspecified",
                "triage_category": "acl_tear",
                "flags": ["urgent", "structural_red_flag", "suspected_ligament_tear"],
            }
        ],
    )
    assert "Red Flag Detected" in text
    assert "Do not train" in text
    forbidden = ("Terminal Knee Extensions", "TKE", "Spanish Squat", "Pogo")
    assert not any(drill in text for drill in forbidden)


def test_generate_rehab_protocols_same_location_entries_merge_injury_types():
    text, _ = generate_rehab_protocols(
        injury_string="",
        exercise_data=[],
        current_phase="GPP",
        parsed_entries=[
            {"canonical_location": "knee", "injury_type": "instability", "rehab_type": "instability", "severity": "moderate"},
            {"canonical_location": "knee", "injury_type": "swelling", "rehab_type": "swelling", "severity": "moderate"},
        ],
    )
    assert "Knee" in text
    assert "Instability" in text
    assert "Swelling" in text


def test_generate_rehab_protocols_same_location_urgent_entry_overrides_nonurgent():
    text, _ = generate_rehab_protocols(
        injury_string="knee pain",
        exercise_data=[],
        current_phase="GPP",
        parsed_entries=[
            {"canonical_location": "knee", "injury_type": "pain", "rehab_type": "pain", "severity": "low"},
            {
                "canonical_location": "knee",
                "injury_type": "unspecified",
                "rehab_type": "unspecified",
                "triage_category": "acl_tear",
                "flags": ["urgent"],
            },
        ],
    )
    assert "Red Flag Detected" in text
    assert "Do not train" in text
    forbidden = ("Terminal Knee Extensions", "TKE", "Spanish Squat", "Pogo")
    assert not any(drill in text for drill in forbidden)


def test_generate_rehab_protocols_keeps_canonical_lookup_when_display_location_is_noncanonical():
    text, _ = generate_rehab_protocols(
        injury_string="",
        exercise_data=[],
        current_phase="GPP",
        parsed_entries=[
            {
                "canonical_location": "knee",
                "display_location": "right knee",
                "injury_type": "instability",
                "rehab_type": "instability",
            },
            {
                "canonical_location": "knee",
                "display_location": "right knee",
                "injury_type": "swelling",
                "rehab_type": "swelling",
            },
        ],
    )
    assert "Knee" in text
    assert "Instability" in text
    assert "Swelling" in text
    assert "Unspecified + " not in text


def test_generate_rehab_protocols_preserves_laterality_for_same_location():
    text, _ = generate_rehab_protocols(
        injury_string="",
        exercise_data=[],
        current_phase="GPP",
        parsed_entries=[
            {"canonical_location": "knee", "laterality": "left", "injury_type": "instability", "rehab_type": "instability"},
            {"canonical_location": "knee", "laterality": "right", "injury_type": "swelling", "rehab_type": "swelling"},
        ],
    )
    assert "- Left Knee" in text
    assert "- Right Knee" in text
    assert "Instability" in text
    assert "Swelling" in text


def test_severe_structural_terms_do_not_parse_as_soft_rehab_buckets(monkeypatch):
    monkeypatch.setattr(injury_synonyms, "get_nlp", lambda: None)

    assert parse_injury_phrase("acl tear") == ("unspecified", "knee")
    assert parse_injury_phrase("tendon rupture") == ("unspecified", None)
    assert parse_injury_phrase("shoulder dislocation") == ("unspecified", "shoulder")
    assert parse_injury_phrase("ankle instability") == ("instability", "ankle")


def test_structural_red_flags_are_exposed_deterministically():
    assert detect_structural_red_flags("acl tear") == [
        "structural_red_flag",
        "suspected_ligament_tear",
        "urgent",
    ]
    assert detect_structural_red_flags("tendon rupture") == [
        "structural_red_flag",
        "suspected_tendon_rupture",
        "urgent",
    ]
    assert detect_structural_red_flags("shoulder dislocation") == [
        "structural_red_flag",
        "suspected_dislocation",
        "urgent",
    ]


def test_parse_injury_entry_keeps_structural_severity_flags(monkeypatch):
    monkeypatch.setattr(injury_synonyms, "get_nlp", lambda: None)

    acl_entry = parse_injury_entry("acl tear")
    assert acl_entry is not None
    assert acl_entry["injury_type"] == "unspecified"
    assert acl_entry["canonical_location"] == "knee"
    assert "structural_red_flag" in acl_entry["flags"]
    assert "suspected_ligament_tear" in acl_entry["flags"]
    assert "urgent" in acl_entry["flags"]

    tendon_entry = parse_injury_entry("tendon rupture")
    assert tendon_entry is not None
    assert tendon_entry["injury_type"] == "unspecified"
    assert tendon_entry["canonical_location"] is None
    assert "suspected_tendon_rupture" in tendon_entry["flags"]
    assert "urgent" in tendon_entry["flags"]

    dislocation_entry = parse_injury_entry("shoulder dislocation")
    assert dislocation_entry is not None
    assert dislocation_entry["injury_type"] == "unspecified"
    assert dislocation_entry["canonical_location"] == "shoulder"
    assert "suspected_dislocation" in dislocation_entry["flags"]
    assert "urgent" in dislocation_entry["flags"]


def test_plan_input_parsed_injury_includes_severity_provenance_defaults():
    from fightcamp.input_parsing import PlanInput
    from tests.support import _build_request

    payload = _build_request().to_payload()
    for field in payload["data"]["fields"]:
        if field.get("label") == "Any injuries or areas you need to work around?":
            field["value"] = "left shoulder pain"
            break

    parsed = PlanInput.from_payload(payload)
    injury = parsed.parsed_injuries[0]

    assert injury["severity"] == "low"
    assert injury["severity_source"] == "injury_type_default"
    assert injury["severity_evidence"] == ["injury type default: pain"]


def test_fallback_default_used_when_no_guided_text_or_injury_type_signal():
    from fightcamp.input_parsing import PlanInput
    from tests.support import _build_request

    payload = _build_request().to_payload()
    for field in payload["data"]["fields"]:
        if field.get("label") == "Any injuries or areas you need to work around?":
            field["value"] = "left shoulder issue"
            break

    parsed = PlanInput.from_payload(payload)
    injury = parsed.parsed_injuries[0]

    assert injury["severity"] == "moderate"
    assert injury["severity_source"] == "fallback_default"
    assert injury["severity_evidence"] == ["fallback default: moderate"]


@pytest.mark.parametrize("injury_text", ["left knee swelling", "left knee instability"])
def test_swelling_and_instability_defaults_are_moderate_without_other_context(injury_text: str):
    from fightcamp.input_parsing import PlanInput
    from tests.support import _build_request

    payload = _build_request().to_payload()
    for field in payload["data"]["fields"]:
        if field.get("label") == "Any injuries or areas you need to work around?":
            field["value"] = injury_text
            break

    parsed = PlanInput.from_payload(payload)
    injury = parsed.parsed_injuries[0]

    assert injury["severity"] == "moderate"
    assert injury["severity_source"] == "injury_type_default"


def test_minor_swelling_with_benign_context_stays_moderate_default():
    from fightcamp.input_parsing import PlanInput
    from tests.support import _build_request

    payload = _build_request().to_payload()
    for field in payload["data"]["fields"]:
        if field.get("label") == "Any injuries or areas you need to work around?":
            field["value"] = "minor swelling, can bear weight, no deformity"
            break

    parsed = PlanInput.from_payload(payload)
    injury = parsed.parsed_injuries[0]

    assert injury["severity"] == "moderate"
    assert injury["severity_source"] == "injury_type_default"


def test_slight_instability_with_no_giving_way_stays_moderate_default():
    from fightcamp.input_parsing import PlanInput
    from tests.support import _build_request

    payload = _build_request().to_payload()
    for field in payload["data"]["fields"]:
        if field.get("label") == "Any injuries or areas you need to work around?":
            field["value"] = "slight ankle instability, stable, no giving way"
            break

    parsed = PlanInput.from_payload(payload)
    injury = parsed.parsed_injuries[0]

    assert injury["severity"] == "moderate"
    assert injury["severity_source"] == "injury_type_default"


def test_build_rehab_injury_string_prefers_structured_knee_instability():
    from types import SimpleNamespace

    from fightcamp.plan_pipeline_blocks import _build_rehab_injury_string

    context = SimpleNamespace(
        plan_input=SimpleNamespace(
            parsed_injuries=[
                {
                    "original_phrase": "Right knee went back as I planted it I think I overextended it",
                    "canonical_location": "knee",
                    "display_location": "right knee",
                    "laterality": "right",
                    "injury_type": "sprain",
                    "severity": "mild",
                    "trend": "stable",
                }
            ],
            guided_injury=SimpleNamespace(
                injury_type="instability / giving way",
                notes="knee gave way on plant",
                area="right knee",
            ),
        ),
        injuries_only_text="Right knee went back as I planted it I think I overextended it",
    )
    # #2867: parsed injuries come from the runtime training context.
    context.training_context = SimpleNamespace(parsed_injuries=context.plan_input.parsed_injuries)

    assert _build_rehab_injury_string(context) == "right knee instability mild stable"


def test_build_rehab_injury_string_falls_back_to_raw_when_no_parsed_entries():
    from types import SimpleNamespace

    from fightcamp.plan_pipeline_blocks import _build_rehab_injury_string

    raw_text = "Right knee went back as I planted it I think I overextended it"
    context = SimpleNamespace(
        plan_input=SimpleNamespace(parsed_injuries=[], guided_injury=None),
        injuries_only_text=raw_text,
    )
    # #2867: parsed injuries come from the runtime training context.
    context.training_context = SimpleNamespace(parsed_injuries=context.plan_input.parsed_injuries)

    assert _build_rehab_injury_string(context) == raw_text

def test_muscle_rupture_gets_specific_structural_flag():
    flags = detect_structural_red_flags("muscle rupture in quad")

    assert "structural_red_flag" in flags
    assert "suspected_muscle_rupture" in flags
    assert "urgent" in flags
    assert "suspected_tendon_rupture" not in flags


def test_tendon_snap_does_not_parse_as_ordinary_strain(monkeypatch):
    monkeypatch.setattr(injury_synonyms, "get_nlp", lambda: None)

    injury_type, location = parse_injury_phrase("achilles tendon snap")

    assert injury_type == "unspecified"
    assert location == "achilles"

    flags = detect_structural_red_flags("achilles tendon snap")
    assert "structural_red_flag" in flags
    assert "suspected_tendon_rupture" in flags
    assert "urgent" in flags


def test_tendon_pop_phrase_is_structural_when_tendon_context_is_clear(monkeypatch):
    monkeypatch.setattr(injury_synonyms, "get_nlp", lambda: None)

    injury_type, location = parse_injury_phrase("felt tendon pop in achilles")

    assert injury_type == "unspecified"
    assert location == "achilles"

    flags = detect_structural_red_flags("felt tendon pop in achilles")
    assert "suspected_tendon_rupture" in flags
    assert "urgent" in flags


def test_black_toenail_does_not_become_generic_contusion(monkeypatch):
    monkeypatch.setattr(injury_synonyms, "get_nlp", lambda: None)

    injury_type, location = parse_injury_phrase("black toenail")

    assert location == "toe"
    assert injury_type != "contusion"


def test_ankle_pop_does_not_resolve_as_soft_sprain(monkeypatch):
    monkeypatch.setattr(injury_synonyms, "get_nlp", lambda: None)

    injury_type, location = parse_injury_phrase("ankle pop")

    # A "pop" can be ligament/bone, so it is not auto-classified as a soft sprain;
    # it stays unspecified for structural-aware routing.
    assert injury_type != "sprain"
    assert location == "ankle"


def test_structural_triage_separates_rehab_and_safety_categories():
    acl = score_injury_phrase("right knee acl tear")
    assert acl["location"] == "knee"
    assert acl["side"] == "right"
    assert acl["triage_category"] == "acl_tear"
    assert "structural_red_flag" in acl["flags"]
    assert "urgent" in acl["flags"]
    assert acl["rehab_type"] == "unspecified"
    assert acl["injury_type"] == "unspecified"

    tendon = score_injury_phrase("achilles tendon rupture")
    assert tendon["location"] == "achilles"
    assert tendon["triage_category"] == "tendon_rupture"
    assert tendon["rehab_type"] == "unspecified"
    assert tendon["injury_type"] == "unspecified"
    assert "urgent" in tendon["flags"]

    dislocation = score_injury_phrase("shoulder dislocation")
    assert dislocation["location"] == "shoulder"
    assert dislocation["triage_category"] == "dislocation"
    assert dislocation["rehab_type"] == "unspecified"
    assert dislocation["injury_type"] == "unspecified"
    assert "structural_red_flag" in dislocation["flags"]

    muscle = score_injury_phrase("muscle rupture in quad")
    assert muscle["location"] == "quads"
    assert muscle["triage_category"] == "muscle_rupture"
    assert "structural_red_flag" in muscle["flags"]
    assert "urgent" in muscle["flags"]

    fracture = score_injury_phrase("fracture in wrist")
    assert fracture["location"] == "wrist"
    assert fracture["triage_category"] == "fracture"
    assert "urgent" in fracture["flags"]


def test_ordinary_rehab_parsing_keeps_injury_and_rehab_types_aligned():
    strain = score_injury_phrase("left calf strain")
    assert strain["injury_type"] == "strain"
    assert strain["rehab_type"] == "strain"
    assert strain["triage_category"] == ""
    assert strain["location"] == "calf"
    assert strain["side"] == "left"

    sprain = score_injury_phrase("rolled ankle")
    assert sprain["injury_type"] == "sprain"
    assert sprain["rehab_type"] == "sprain"
    assert sprain["triage_category"] == ""
    assert sprain["location"] == "ankle"

    tendinopathy = score_injury_phrase("right knee patellar tendinopathy")
    assert tendinopathy["injury_type"] == "tendonitis"
    assert tendinopathy["rehab_type"] == "tendonitis"
    assert tendinopathy["triage_category"] == ""
    assert tendinopathy["location"] == "knee"


def test_rehab_lookup_handles_old_entries_without_rehab_type():
    from fightcamp.rehab_protocols import _normalize_existing_injury_entries

    entries = _normalize_existing_injury_entries(
        [
            {
                "injury_type": "sprain",
                "canonical_location": "ankle",
                "laterality": "left",
                "severity": "mild",
                "original_phrase": "rolled ankle",
            }
        ]
    )
    assert entries[0]["rehab_type"] == "sprain"


def test_build_rehab_injury_string_uses_entry_level_guided_types_without_leakage():
    from types import SimpleNamespace

    from fightcamp.plan_pipeline_blocks import _build_rehab_injury_string

    context = SimpleNamespace(
        plan_input=SimpleNamespace(
            parsed_injuries=[
                {
                    "canonical_location": "knee",
                    "display_location": "right knee",
                    "laterality": "right",
                    "injury_type": "sprain",
                    "guided_source_injury_type": "instability / giving way",
                },
                {
                    "canonical_location": "shoulder",
                    "display_location": "left shoulder",
                    "laterality": "left",
                    "injury_type": "sprain",
                    "guided_source_injury_type": "pain",
                },
            ],
            guided_injury=SimpleNamespace(injury_type="instability / giving way"),
        ),
        injuries_only_text="",
    )
    # #2867: parsed injuries come from the runtime training context.
    context.training_context = SimpleNamespace(parsed_injuries=context.plan_input.parsed_injuries)

    injury_string = _build_rehab_injury_string(context)

    assert "right knee instability" in injury_string
    assert "left shoulder pain" in injury_string
    assert "left shoulder instability" not in injury_string


def test_generate_rehab_support_bundle_counts_guided_only_parsed_injury_as_injury(monkeypatch):
    from types import SimpleNamespace

    from fightcamp.plan_pipeline_blocks import (
        _build_rehab_injury_string,
        _generate_rehab_support_bundle,
    )

    monkeypatch.setattr(
        "fightcamp.plan_pipeline_blocks.generate_rehab_protocols",
        lambda **kwargs: ("rehab block", []),
    )
    monkeypatch.setattr(
        "fightcamp.plan_pipeline_blocks.format_injury_guardrails",
        lambda *args, **kwargs: "guardrail",
    )
    monkeypatch.setattr(
        "fightcamp.plan_pipeline_blocks.generate_nutrition_block",
        lambda flags: "nutrition",
    )
    monkeypatch.setattr(
        "fightcamp.plan_pipeline_blocks.generate_support_notes",
        lambda injury_string: f"support for {injury_string}",
    )

    context = SimpleNamespace(
        plan_input=SimpleNamespace(
            parsed_injuries=[
                {
                    "canonical_location": "knee",
                    "display_location": "right knee",
                    "injury_type": "instability",
                }
            ],
            injuries="",
            restrictions=[],
        ),
        injuries_only_text="",
        phase_active=lambda phase: phase == "GPP",
        phase_weeks={"GPP": 1, "SPP": 0, "TAPER": 0, "days": {"GPP": 7, "SPP": 0, "TAPER": 0}},
        training_context=SimpleNamespace(to_flags=lambda: {}),
        exercise_bank=[],
        apply_muay_thai_filters=False,
        sanitize_labels=(),
    )
    # #2867: parsed injuries come from the runtime training context.
    context.training_context.parsed_injuries = context.plan_input.parsed_injuries
    context.training_context.injuries_raw_text = context.plan_input.injuries
    context.training_context.injury_restrictions = context.plan_input.restrictions

    # The pipeline builds the rehab injury string once and hands it to the bundle.
    _, _, support_notes, has_injuries, *_ = _generate_rehab_support_bundle(
        context, _build_rehab_injury_string(context)
    )

    assert has_injuries is True
    assert support_notes.startswith("support for")


def test_instability_beats_sprain_with_giving_way_language():
    result = score_injury_phrase("ankle sprain with giving way")
    assert result["injury_type"] == "instability"
    assert result["location"] == "ankle"
    assert result["triage_category"] == ""


def test_impingement_clicking_alone_is_not_confident():
    shoulder_clicking = score_injury_phrase("shoulder clicking no pain")
    hip_clicking = score_injury_phrase("hip clicking no pain")
    assert shoulder_clicking["injury_type"] != "impingement"
    assert hip_clicking["injury_type"] != "impingement"


def test_impingement_gate_hints_allow_classification():
    shoulder_pinching = score_injury_phrase("shoulder pinching when raising arm")
    hip_catching = score_injury_phrase("hip catching with painful range")
    assert shoulder_pinching["injury_type"] == "impingement"
    assert hip_catching["injury_type"] == "impingement"


def test_soreness_stiffness_tightness_beat_generic_pain_when_context_is_clear():
    assert score_injury_phrase("quad soreness after training")["injury_type"] == "soreness"
    assert score_injury_phrase("knee stiff in the morning")["injury_type"] == "stiffness"
    assert score_injury_phrase("hamstring tight and loosens after warmup")["injury_type"] == "tightness"
    assert score_injury_phrase("shoulder pain")["injury_type"] == "pain"


def test_tendonitis_requires_tendon_overuse_context_to_beat_pain():
    assert score_injury_phrase("patellar tendon pain recurring after jumping")["injury_type"] == "tendonitis"
    assert score_injury_phrase("achilles tendinopathy")["injury_type"] == "tendonitis"
    assert score_injury_phrase("knee pain")["injury_type"] == "pain"
    assert score_injury_phrase("shoulder pain")["injury_type"] == "pain"


def test_joint_instability_does_not_default_to_ankle_region():
    injuries, restrictions = parse_injuries_and_restrictions("joint instability")

    assert restrictions == []
    assert len(injuries) == 1
    assert injuries[0].get("injury_type") == "instability"
    assert injuries[0].get("canonical_location") in {None, "", "unspecified"}


def test_ankle_instability_and_sprain_still_map_to_ankle():
    injuries, _ = parse_injuries_and_restrictions("ankle instability and ankle sprain")
    locations = {entry.get("canonical_location") for entry in injuries}
    assert "ankle" in locations


def test_score_and_canonicalize_injury_type_agree_on_ambiguous_phrases():
    if not negation_detection_available():
        pytest.skip("spaCy/NegEx not available; canonical fallback path remains legacy")
    phrases = [
        "ankle sprain with giving way",
        "shoulder clicking no pain",
        "shoulder pinching when raising arm",
        "quad soreness after training",
        "knee stiff in the morning",
        "hamstring tight and loosens after warmup",
        "patellar tendon pain recurring after jumping",
        "knee pain",
        "shoulder pain",
    ]
    for phrase in phrases:
        assert canonicalize_injury_type(phrase) == score_injury_phrase(phrase)["injury_type"]


def test_generate_rehab_protocols_prefers_structured_entries():
    result, _ = generate_rehab_protocols(
        injury_string="knee pain",
        parsed_entries=[{"canonical_location": "knee", "injury_type": "instability"}],
        exercise_data=[],
        current_phase="GPP",
    )
    assert "- Knee Instability:" in result
    assert "Knee (Pain)" not in result
    assert "- Knee Pain:" not in result
    assert "  • " not in result


def test_generate_rehab_protocols_structured_entries_do_not_bleed_across_regions():
    result, _ = generate_rehab_protocols(
        injury_string="knee instability",
        parsed_entries=[{"canonical_location": "shoulder", "injury_type": "pain"}],
        exercise_data=[],
        current_phase="GPP",
    )
    # #3033: published pain guidance uses an informational-summary heading.
    assert "- Shoulder Pain:" in result
    assert "Knee (Instability)" not in result
    assert "knee" not in result.lower()
    assert "  • " not in result


def test_generate_rehab_protocols_string_fallback_still_works():
    result, _ = generate_rehab_protocols(
        injury_string="knee instability",
        exercise_data=[],
        current_phase="GPP",
    )
    assert "- Knee Instability:" in result
    assert "  • " not in result


def test_generate_rehab_protocols_structured_entry_uses_legacy_injury_type_field():
    result, _ = generate_rehab_protocols(
        injury_string="",
        parsed_entries=[{"location": "knee", "injury_type": "pain"}],
        exercise_data=[],
        current_phase="GPP",
    )
    assert "- Knee Pain:" in result
    assert "  • " not in result


def test_generate_rehab_protocols_structured_multiple_injuries_render_separately():
    result, _ = generate_rehab_protocols(
        injury_string="",
        parsed_entries=[
            {"canonical_location": "knee", "injury_type": "instability"},
            {"canonical_location": "shoulder", "injury_type": "pain"},
        ],
        exercise_data=[],
        current_phase="GPP",
    )
    assert "- Knee Instability:" in result
    assert "- Shoulder Pain:" in result
    assert sum(line.startswith("- ") for line in result.splitlines()) == 2
    assert "  • " not in result


def test_rehab_severity_filter_high_achilles_blocks_aggressive_terms():
    result, _ = generate_rehab_protocols(
        injury_string="",
        parsed_entries=[{"canonical_location": "achilles", "injury_type": "tendonitis", "severity": "severe"}],
        exercise_data=[],
        current_phase="SPP",
    )
    forbidden = ("pogo", "hop", "jump", "sprint", "bfr", "depth", "drop")
    assert not any(token in result.lower() for token in forbidden)


def test_rehab_severity_filter_high_knee_instability_blocks_aggressive_terms():
    result, _ = generate_rehab_protocols(
        injury_string="",
        parsed_entries=[{"canonical_location": "knee", "injury_type": "instability", "severity": "high"}],
        exercise_data=[],
        current_phase="SPP",
    )
    forbidden = ("pogo", "reactive", "jump", "hard cutting", "bfr")
    assert not any(token in result.lower() for token in forbidden)


def test_rehab_severity_filter_moderate_blocks_heavy_and_sprint():
    result, _ = generate_rehab_protocols(
        injury_string="",
        parsed_entries=[{"canonical_location": "knee", "injury_type": "instability", "severity": "moderate"}],
        exercise_data=[],
        current_phase="SPP",
    )
    assert "sprint" not in result.lower()
    assert "heavy" not in result.lower()


def test_rehab_day_type_specific_why_and_volume_limits():
    from fightcamp.rehab_clinical import load_clinical_policies
    from fightcamp.stage2_payload import _build_rehab_slots
    policy = next(p for p in load_clinical_policies() if p.policy_id == "ankle_sprain")
    assert policy.activation == "live"
    # #2984: exercise coaching requires a published exercise decision.
    entries = [{"canonical_location": "ankle", "injury_type": "sprain", "severity": "moderate",
                "rehab_stage": "restore", "laterality": "left", "available_equipment": ["stable_support"]}]
    sparring_text, _ = generate_rehab_protocols(
        injury_string="ankle sprain",
        parsed_entries=entries,
        exercise_data=[],
        current_phase="GPP",
        day_type="sparring",
    )
    strength_text, _ = generate_rehab_protocols(
        injury_string="ankle sprain",
        parsed_entries=entries,
        exercise_data=[],
        current_phase="GPP",
        day_type="strength",
    )
    assert "pre-sparring inclusion" in sparring_text
    assert "main lift" in strength_text
    assert sparring_text.count("  • ") <= 1
    slots = _build_rehab_slots(sparring_text, "GPP")
    assert len(slots) == 1
    assert slots[0]["selected"]["rehab_drill_id"] in {
        rx.drill_id for rx in policy.prescriptions if rx.stage == "restore"
    }


def test_phase_level_plan_does_not_use_day_specific_rehab_why():
    from tests.support import _build_request
    generate_plan_sync = pytest.importorskip("fightcamp.main").generate_plan_sync
    request = _build_request({"injuries": "right knee pain"}).to_payload()
    request["random_seed"] = 3
    result = generate_plan_sync(request)
    plan_text = result.get("plan_text", "")
    assert "pre-sparring inclusion" not in plan_text


@pytest.mark.parametrize(
    "injury,forbidden_terms",
    [
        ("right knee ACL tear", ("terminal knee extensions", "tke", "spanish squat", "pogo", "sprint", "depth jump", "hard cutting", "heavy squat", "max velocity", "repeated plyos")),
        ("left achilles tendon rupture", ("eccentric calf drops", "tip-toe holds", "pogo", "hops", "sprint", "jump")),
        ("right shoulder dislocation", ("overhead press", "push press", "snatch", "jerk", "dips", "heavy pressing", "explosive upper push")),
    ],
)
def test_structural_injury_end_to_end_blocks_unsafe_output(injury, forbidden_terms):
    from tests.support import _build_request
    generate_plan_sync = pytest.importorskip("fightcamp.main").generate_plan_sync
    request = _build_request({"injuries": injury}).to_payload()
    request["random_seed"] = 17
    result = generate_plan_sync(request)
    text = (result.get("plan_text") or "").lower()
    # Canonical safety marker after the injury-triage block: "## Injury Triage:"
    # heading prefix; older fixtures looked for "red flag detected" which the
    # blocked_mode_output template no longer emits.
    assert "injury triage:" in text
    assert not any(term in text for term in forbidden_terms)


def test_concussion_end_to_end_blocks_contact_and_high_cns_output():
    from tests.support import _build_request
    generate_plan_sync = pytest.importorskip("fightcamp.main").generate_plan_sync
    request = _build_request({"injuries": "concussion last week"}).to_payload()
    request["random_seed"] = 17
    result = generate_plan_sync(request)
    text = (result.get("plan_text") or "").lower()
    assert "injury triage:" in text
    assert "sparring" not in text
    for forbidden in (
        "contact",
        "live rounds",
        "hard rounds",
        "max velocity",
        "sprint intervals",
        "high-cns",
        "explosive conditioning",
    ):
        assert forbidden not in text


def test_score_injury_phrase_concussion_is_triaged_urgent_structural():
    scored = score_injury_phrase("concussion last week")
    assert scored["triage_category"] == "concussion"
    assert "urgent" in scored["flags"]
    assert "structural_red_flag" in scored["flags"]
    assert "suspected_concussion" in scored["flags"]


def test_parse_injury_entry_head_injury_sets_concussion_flags():
    entry = parse_injury_entry("head injury from sparring")
    assert entry is not None
    assert entry["triage_category"] == "concussion"
    assert "urgent" in entry["flags"]
    assert "structural_red_flag" in entry["flags"]
    assert "suspected_concussion" in entry["flags"]


def test_generate_rehab_protocols_concussion_returns_red_flag_no_drills():
    text, seen = generate_rehab_protocols(
        injury_string="concussion",
        current_phase="GPP",
        parsed_entries=[parse_injury_entry("concussion")],
        exercise_data=[],
        day_type="strength",
    )
    normalized = text.lower()
    assert "red flag detected" in normalized
    assert "  • " not in text
    assert seen == set()


def test_danger_phrase_does_not_parse_as_normal_sprain():
    parsed_type, _ = parse_injury_phrase("shoulder popped out")
    assert parsed_type != "sprain"


def test_out_of_socket_not_treated_as_ordinary_instability_or_sprain():
    parsed_type, _ = parse_injury_phrase("out of socket")
    assert parsed_type not in {"sprain", "instability"}


def test_danger_term_boundary_does_not_match_popliteus_word_fragment():
    assert detect_danger_term_routes("popliteus soreness after run") == []


def test_non_injury_snapping_phrase_does_not_trigger_danger_routing():
    assert detect_danger_term_routes("snapping turtle at the pond") == []
