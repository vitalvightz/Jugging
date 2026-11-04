import re
from datetime import date, datetime
import logging

import pytest

from fightcamp import input_parsing
from fightcamp.fight_date_utils import parse_fight_date
from fightcamp.injury_formatting import format_injury_summary
from fightcamp.input_parsing import GuidedInjury, PlanInput
from fightcamp.plan_pipeline_runtime import build_runtime_context


def _payload(fields: list[dict]) -> dict:
    return {"data": {"fields": fields}}


def test_invalid_training_frequency_raises_value_error():
    data = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Weekly Training Frequency", "value": "abc"},
            {"label": "Training Availability", "value": "Mon, Wed"},
        ]
    )
    with pytest.raises(ValueError, match="invalid Weekly Training Frequency"):
        PlanInput.from_payload(data)


def test_missing_fight_date_sets_na():
    data = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Training Availability", "value": "Mon, Wed, Fri"},
        ]
    )
    parsed = PlanInput.from_payload(data)
    assert parsed.weeks_out == "N/A"
    assert parsed.days_until_fight is None


def test_style_parsing_lowercases():
    data = _payload(
        [
            {"label": "Fighting Style (Technical)", "value": "Boxing, MMA"},
            {"label": "Fighting Style (Tactical)", "value": "Pressure Fighter"},
        ]
    )
    parsed = PlanInput.from_payload(data)
    assert parsed.tech_styles == ["boxing", "mma"]
    assert parsed.tactical_styles == ["pressure fighter"]


def test_past_fight_date_handling_is_explicit():
    data = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "When is your next fight?", "value": "2000-01-01"},
        ]
    )
    parsed = PlanInput.from_payload(data)
    assert parsed.days_until_fight is None
    assert parsed.weeks_out == "N/A"


def test_same_day_fight_date_remains_fight_week_active(monkeypatch):
    today = "2026-03-14"
    monkeypatch.setattr(input_parsing, "_utc_now", lambda: datetime(2026, 3, 14, 0, 30))
    data = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "When is your next fight?", "value": today},
        ]
    )
    parsed = PlanInput.from_payload(data)
    assert parsed.days_until_fight == 0
    assert parsed.weeks_out == 1


def test_field_alias_matching_for_key_inputs():
    data = _payload(
        [
            {"label": "Fight date", "value": "2099-01-20"},
            {"label": "Technical style", "value": "Boxing"},
            {"label": "Tactical style", "value": "Pressure Fighter"},
            {"label": "Training frequency", "value": "3"},
            {"label": "Available training days", "value": "Mon, Wed, Fri"},
            {"label": "Current injuries", "value": "wrist soreness"},
        ]
    )
    parsed = PlanInput.from_payload(data)
    assert parsed.next_fight_date == "2099-01-20"
    assert parsed.tech_styles == ["boxing"]
    assert parsed.tactical_styles == ["pressure fighter"]
    assert parsed.training_frequency == 3
    assert parsed.training_days == ["Mon", "Wed", "Fri"]
    assert parsed.injuries == "wrist soreness"


def test_whitespace_case_insensitive_label_matching():
    data = _payload(
        [
            {"label": "  weekly training frequency  ", "value": "2"},
            {"label": "  training availability  ", "value": "Tue, Thu"},
            {"label": "  when IS your NEXT fight?  ", "value": "2099-02-01"},
        ]
    )
    parsed = PlanInput.from_payload(data)
    assert parsed.training_frequency == 2
    assert parsed.training_days == ["Tue", "Thu"]
    assert parsed.next_fight_date == "2099-02-01"


def test_exact_match_still_preferred_over_alias():
    data = _payload(
        [
            {"label": "Fight date", "value": "2099-02-01"},
            {"label": "When is your next fight?", "value": "2099-03-01"},
            {"label": "Training frequency", "value": "2"},
            {"label": "Weekly Training Frequency", "value": "5"},
        ]
    )
    parsed = PlanInput.from_payload(data)
    assert parsed.next_fight_date == "2099-03-01"
    assert parsed.training_frequency == 5


def test_payload_requires_fields_list():
    with pytest.raises(ValueError, match=re.escape("payload missing required data.fields list")):
        PlanInput.from_payload({"data": {}})


def test_multiselect_value_maps_when_option_ids_are_strings():
    data = _payload(
        [
            {
                "label": "Training Availability",
                "value": [1, 3],
                "options": [
                    {"id": "1", "text": "Mon"},
                    {"id": "2", "text": "Tue"},
                    {"id": "3", "text": "Fri"},
                ],
            }
        ]
    )
    parsed = PlanInput.from_payload(data)
    assert parsed.available_days == "Mon, Fri"
    assert parsed.training_days == ["Mon", "Fri"]


@pytest.mark.parametrize(
    "haystack, needle, expected",
    [
        # Short location terms must not be matched inside unrelated words.
        ("chipped tooth", "hip", False),
        ("warm up shoulders", "arm", False),
        ("forearm strain", "ear", False),
        # Genuine whole-phrase duplicates are still skipped.
        ("knee swelling", "swelling", True),
        ("left hip pain", "hip", True),
    ],
)
def test_phrase_present_is_word_boundary_safe(haystack, needle, expected):
    assert input_parsing._phrase_present(haystack, needle) is expected


def test_guided_severity_wins_when_text_is_not_more_severe():
    payload = _payload(
        [
            {"label": "Any injuries or areas you need to work around?", "value": ""},
        ]
    )
    payload["guided_injury"] = {
        "area": "left knee swelling",
        "severity": "high",
        "trend": "stable",
        "notes": "swelling but walking fine",
    }

    parsed = PlanInput.from_payload(payload)
    injury = parsed.parsed_injuries[0]

    assert injury["severity"] == "high"
    assert injury["severity_source"] == "guided_card"


def test_text_detected_wins_when_no_guided_severity():
    payload = _payload(
        [
            {"label": "Any injuries or areas you need to work around?", "value": ""},
        ]
    )
    payload["guided_injury"] = {
        "area": "left knee swelling",
        "trend": "stable",
        "notes": "sharp pain and cannot bear weight",
    }

    parsed = PlanInput.from_payload(payload)
    injury = parsed.parsed_injuries[0]

    assert injury["severity"] == "high"
    assert injury["severity_source"] == "text_detected"


def test_swelling_stays_injury_type_default_without_escalation_context():
    parsed = PlanInput.from_payload(
        _payload([{"label": "Any injuries or areas you need to work around?", "value": "left knee swelling"}])
    )
    injury = parsed.parsed_injuries[0]
    assert injury["severity"] == "moderate"
    assert injury["severity_source"] == "injury_type_default"
    assert injury["severity_evidence"] == ["injury type default: swelling"]


def test_instability_stays_injury_type_default_when_no_instability_event():
    parsed = PlanInput.from_payload(
        _payload(
            [
                {
                    "label": "Any injuries or areas you need to work around?",
                    "value": "slight ankle instability, stable, no giving way",
                }
            ]
        )
    )
    injury = parsed.parsed_injuries[0]
    assert injury["severity"] == "moderate"
    assert injury["severity_source"] == "injury_type_default"


@pytest.mark.parametrize(
    "notes",
    [
        "ankle swollen after tackle",
        "knee puffy after impact",
        "inflamed after collision",
    ],
)
def test_swelling_family_terms_with_trauma_context_escalate_to_high(notes: str):
    payload = _payload([{"label": "Any injuries or areas you need to work around?", "value": ""}])
    payload["guided_injury"] = {"area": "left knee swelling", "trend": "stable", "notes": notes}

    parsed = PlanInput.from_payload(payload)
    injury = parsed.parsed_injuries[0]

    assert injury["severity"] == "high"
    assert injury["severity_source"] == "text_detected"


def test_equipment_multiselect_value_maps_from_option_ids():
    data = _payload(
        [
            {
                "label": "Equipment Access",
                "value": [2, 3],
                "options": [
                    {"id": "1", "text": "Barbell"},
                    {"id": "2", "text": "Bands"},
                    {"id": "3", "text": "Heavy Bag"},
                ],
            }
        ]
    )

    parsed = PlanInput.from_payload(data)

    assert parsed.equipment_access == "Bands, Heavy Bag"


def test_legacy_payload_without_primary_fields_defaults_to_first_selected_values():
    data = _payload(
        [
            {"label": "What are your key performance goals?", "value": "power, mobility"},
            {"label": "Where do you feel weakest right now?", "value": "cns_fatigue, hip_mobility"},
        ]
    )

    parsed = PlanInput.from_payload(data)
    assert parsed.primary_goal == "power"
    assert parsed.primary_weak_area == "cns_fatigue"


def test_valid_primary_fields_are_preserved():
    data = _payload(
        [
            {"label": "What are your key performance goals?", "value": "power, conditioning, mobility"},
            {"label": "Primary goal", "value": "conditioning"},
            {"label": "Where do you feel weakest right now?", "value": "cns_fatigue, hip_mobility"},
            {"label": "Primary weak area", "value": "hip_mobility"},
        ]
    )

    parsed = PlanInput.from_payload(data)
    assert parsed.primary_goal == "conditioning"
    assert parsed.primary_weak_area == "hip_mobility"


def test_collision_clarification_fields_parse_when_present():
    data = _payload(
        [
            {"label": "What are your key performance goals?", "value": "power, conditioning"},
            {"label": "Primary goal", "value": "power"},
            {"label": "Where do you feel weakest right now?", "value": "power, gas_tank"},
            {"label": "Primary weak area", "value": "power"},
            {"label": "Goal/weak-area collision tags", "value": ["power"]},
            {"label": "Goal/weak-area collision detail", "value": "Power drops when tired"},
        ]
    )

    parsed = PlanInput.from_payload(data)

    assert parsed.goal_weakness_collision_tags == ["power"]
    assert parsed.goal_weakness_collision_detail == "Power drops when tired"
    assert parsed.primary_goal == "power"
    assert parsed.primary_weak_area == "power"


def test_collision_clarification_fields_default_when_missing_or_empty():
    missing = PlanInput.from_payload(
        _payload(
            [
                {"label": "What are your key performance goals?", "value": "power"},
                {"label": "Where do you feel weakest right now?", "value": "power"},
            ]
        )
    )
    empty = PlanInput.from_payload(
        _payload(
            [
                {"label": "Goal/weak-area collision tags", "value": ""},
                {"label": "Goal/weak-area collision detail", "value": ""},
            ]
        )
    )

    assert missing.goal_weakness_collision_tags == []
    assert missing.goal_weakness_collision_detail == ""
    assert empty.goal_weakness_collision_tags == []
    assert empty.goal_weakness_collision_detail == ""


def test_collision_clarification_details_parse_from_raw_list_and_json_string():
    list_payload = _payload(
        [
            {
                "label": "Goal/weak-area collision details",
                "value": [
                    {"tag": " power ", "label": " Power ", "detail": " Drops when tired "},
                    {"tag": "", "label": "Conditioning", "detail": "Late-round fatigue"},
                    {"tag": "", "label": "", "detail": ""},
                ],
            }
        ]
    )
    json_payload = _payload(
        [
            {
                "label": "Goal/weak-area collision details",
                "value": '[{"tag":"conditioning","label":"Conditioning","detail":" Recovery between bursts "}]',
            }
        ]
    )

    parsed_list = PlanInput.from_payload(list_payload)
    parsed_json = PlanInput.from_payload(json_payload)

    assert parsed_list.goal_weakness_collision_details == [
        {"tag": "power", "label": "Power", "detail": "Drops when tired"},
        {"tag": "", "label": "Conditioning", "detail": "Late-round fatigue"},
    ]
    assert parsed_json.goal_weakness_collision_details == [
        {"tag": "conditioning", "label": "Conditioning", "detail": "Recovery between bursts"}
    ]


def test_collision_clarification_details_default_empty_on_malformed_values():
    malformed = _payload(
        [
            {"label": "Goal/weak-area collision details", "value": "not-json"},
        ]
    )

    parsed = PlanInput.from_payload(malformed)
    assert parsed.goal_weakness_collision_details == []


def test_invalid_primary_fields_fall_back_to_first_selected_values():
    data = _payload(
        [
            {"label": "What are your key performance goals?", "value": "power, mobility"},
            {"label": "Primary goal", "value": "conditioning"},
            {"label": "Where do you feel weakest right now?", "value": "cns_fatigue"},
            {"label": "Primary weak area", "value": "grip_strength"},
        ]
    )

    parsed = PlanInput.from_payload(data)
    assert parsed.primary_goal == "power"
    assert parsed.primary_weak_area == "cns_fatigue"


def test_sparring_day_fields_round_trip_from_payload():
    data = _payload(
        [
            {"label": "Training Availability", "value": "Monday, Tuesday, Thursday, Saturday"},
            {"label": "Hard Sparring Days", "value": "Tuesday, Saturday"},
        {"label": "Support Work Days", "value": "Monday"},
        ]
    )

    parsed = PlanInput.from_payload(data)

    assert parsed.training_days == ["Monday", "Tuesday", "Thursday", "Saturday"]
    assert parsed.hard_sparring_days == ["Tuesday", "Saturday"]
    assert parsed.support_work_days == ["Monday"]


def test_contradictory_frequency_and_availability_raise_value_error():
    data = _payload(
        [
            {"label": "Weekly Training Frequency", "value": "6"},
            {"label": "Training Availability", "value": "Mon, Wed"},
            {"label": "When is your next fight?", "value": "2099-03-01"},
        ]
    )

    with pytest.raises(
        ValueError,
        match="invalid Weekly Training Frequency: cannot exceed selected Training Availability days",
    ):
        PlanInput.from_payload(data)


def test_messy_injury_input_keeps_real_issue_and_discards_empty_markers():
    data = _payload(
        [
            {"label": "Any injuries or areas you need to work around?", "value": "none / right heel soreness + toe pain"},
        ]
    )

    parsed = PlanInput.from_payload(data)

    assert parsed.injuries == "right heel soreness, toe pain"
    assert parsed.parsed_injuries


def test_incomplete_input_can_still_be_salvaged_when_training_days_exist():
    data = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Training Availability", "value": "Monday, Thursday, Saturday"},
            {"label": "Any injuries or areas you need to work around?", "value": "n/a"},
        ]
    )

    parsed = PlanInput.from_payload(data)

    assert parsed.training_frequency == 3
    assert parsed.injuries == ""


def test_guided_injury_payload_treats_area_as_source_of_truth():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "hip flexor (moderate, improving). Avoid: deep hip flexion. Notes: pain when driving knee up past pelvis"},
        ]
    )
    payload["guided_injury"] = {
        "area": "hip flexor",
        "severity": "moderate",
        "trend": "improving",
        "avoid": "deep hip flexion",
        "notes": "pain when driving knee up past pelvis",
    }

    parsed = PlanInput.from_payload(payload)

    assert parsed.guided_injury is not None
    assert len(parsed.parsed_injuries) == 1
    assert parsed.parsed_injuries[0]["canonical_location"] == "hip"
    assert parsed.parsed_injuries[0]["display_location"] == "hip flexor"
    assert parsed.parsed_injuries[0]["severity"] == "moderate"
    assert len(parsed.restrictions) == 1
    assert parsed.restrictions[0]["region"] == "hip"


def test_guided_injuries_payload_parses_multiple_cards_and_preserves_notes():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {
                "label": "Any injuries or areas you need to work around?",
                "value": "hip flexor (moderate, improving). Right heel. Notes: roadwork flare-up.",
            },
        ]
    )
    payload["guided_injuries"] = [
        {
            "area": "hip flexor",
            "severity": "moderate",
            "trend": "improving",
            "avoid": "deep hip flexion",
            "notes": "pain when driving knee up past pelvis",
        },
        {
            "area": "right heel",
            "severity": "low",
            "trend": "stable",
            "notes": "roadwork flare-up",
        },
    ]

    parsed = PlanInput.from_payload(payload)

    assert parsed.guided_injury is not None
    assert parsed.guided_injury.area == "hip flexor"
    assert len(parsed.parsed_injuries) == 2
    assert parsed.parsed_injuries[0]["display_location"] == "hip flexor"
    assert parsed.parsed_injuries[0]["notes"] == "pain when driving knee up past pelvis"
    assert parsed.parsed_injuries[1]["display_location"] == "heel"
    assert parsed.parsed_injuries[1]["notes"] == "roadwork flare-up"
    assert parsed.parsed_injuries[0]["guided_source_injury_type"] == ""
    assert parsed.parsed_injuries[0]["guided_source_area"] == "hip flexor"
    assert parsed.parsed_injuries[1]["guided_source_injury_type"] == ""
    assert parsed.parsed_injuries[1]["guided_source_area"] == "right heel"
    assert len(parsed.restrictions) == 1
    assert parsed.restrictions[0]["region"] == "hip"
    assert len(parsed.guided_injuries) == 2


def test_plan_input_preserves_multiple_guided_injuries_and_backwards_alias():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Any injuries or areas you need to work around?", "value": ""},
        ]
    )
    payload["guided_injuries"] = [
        {
            "area": "rolled left ankle",
            "injury_type": "tendon_ligament",
            "severity": "moderate",
            "trend": "stable",
            "notes": "Can bear weight. No deformity.",
        },
        {
            "area": "head impact",
            "injury_type": "impact",
            "severity": "moderate",
            "trend": "stable",
            "notes": "Vomited after head impact.",
        },
    ]

    parsed = PlanInput.from_payload(payload)

    assert len(parsed.guided_injuries) == 2
    assert parsed.guided_injury == parsed.guided_injuries[0]
    assert len(parsed.parsed_injuries) == 2


def test_legacy_guided_injury_populates_guided_injuries_list():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Any injuries or areas you need to work around?", "value": ""},
        ]
    )
    payload["guided_injury"] = {
        "area": "left wrist tightness",
        "severity": "mild",
        "trend": "stable",
    }

    parsed = PlanInput.from_payload(payload)

    assert parsed.guided_injury is not None
    assert len(parsed.guided_injuries) == 1
    assert parsed.guided_injuries[0] == parsed.guided_injury


def test_guided_injuries_are_parsed_once(monkeypatch):
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "hip flexor"},
        ]
    )
    payload["guided_injuries"] = [{"area": "hip flexor", "severity": "moderate"}]
    calls = 0
    original = input_parsing._parse_guided_injuries

    def counting_parse(guided_injuries):
        nonlocal calls
        calls += 1
        return original(guided_injuries)

    monkeypatch.setattr(input_parsing, "_parse_guided_injuries", counting_parse)

    parsed = PlanInput.from_payload(payload)

    assert calls == 1
    assert parsed.guided_injury is not None
    assert parsed.guided_injury.area == "hip flexor"


def test_free_text_injury_fallback_still_parses_without_guided_injury():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "right knee soreness. Avoid: deep knee flexion"},
        ]
    )

    parsed = PlanInput.from_payload(payload)

    assert parsed.guided_injury is None
    assert parsed.parsed_injuries
    assert parsed.restrictions


@pytest.mark.parametrize(
    ("body_map_area", "expected_canonical"),
    [
        ("Head / Neck", "neck"),
        ("Upper back", "upper back"),
        ("Lower back", "lower back"),
        ("Left glute", "glute"),
        ("Right quad", "quads"),
        ("Core", "core"),
        # Zones added with the muscles / joints & bones body-map layers.
        ("Left bicep", "biceps"),
        ("Right tricep", "triceps"),
        ("Left forearm", "forearm"),
        ("Left hand", "hand"),
        ("Groin", "groin"),
        ("Traps", "upper back"),
        ("Left Achilles", "achilles"),
        ("Left foot", "foot"),
        ("Ribs", "chest"),
    ],
)
def test_body_map_area_labels_map_to_backend_canonical_locations(body_map_area: str, expected_canonical: str):
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": ""},
        ]
    )
    payload["guided_injury"] = {
        "area": body_map_area,
        "severity": "moderate",
        "trend": "stable",
        "avoid": "",
        "notes": "",
    }

    parsed = PlanInput.from_payload(payload)

    assert parsed.parsed_injuries[0]["canonical_location"] == expected_canonical


def test_guided_avoid_field_is_parsed_into_restriction_with_attached_region():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": ""},
        ]
    )
    payload["guided_injury"] = {
        "area": "Left shoulder",
        "severity": "moderate",
        "trend": "stable",
        "avoid": "heavy overhead pressing",
        "notes": "",
    }

    parsed = PlanInput.from_payload(payload)

    assert len(parsed.restrictions) == 1
    assert parsed.restrictions[0]["restriction"] == "heavy_overhead_pressing"
    assert parsed.restrictions[0]["region"] == "shoulder"


def test_guided_injuries_override_conflicting_guided_injury_for_consistency():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "right heel, hip flexor"},
        ]
    )
    payload["guided_injuries"] = [
        {"area": "right heel", "notes": "roadwork flare-up"},
        {"area": "hip flexor", "severity": "moderate"},
    ]
    payload["guided_injury"] = {"area": "left shoulder", "severity": "high"}

    parsed = PlanInput.from_payload(payload)

    assert parsed.guided_injury is not None
    assert parsed.guided_injury.area == "right heel"
    assert [entry["display_location"] for entry in parsed.parsed_injuries] == ["heel", "hip flexor"]


def test_extract_guided_injuries_ignores_non_dict_nested_data():
    injuries = input_parsing._extract_guided_injuries({"data": None, "guided_injuries": [{"area": "right heel"}]})

    assert len(injuries) == 1
    assert injuries[0].area == "right heel"


def test_guided_injury_runtime_context_does_not_leak_note_body_parts():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "hip flexor (moderate, improving). Avoid: deep hip flexion. Notes: pain when driving knee up past pelvis"},
        ]
    )
    payload["guided_injury"] = {
        "area": "hip flexor",
        "severity": "moderate",
        "trend": "improving",
        "avoid": "deep hip flexion",
        "notes": "pain when driving knee up past pelvis",
    }

    parsed = PlanInput.from_payload(payload)
    context = build_runtime_context(
        plan_input=parsed,
        random_seed=None,
        logger=logging.getLogger(__name__),
    )

    assert context.injuries_only_text == "hip flexor"
    # #3117 (8ec8e8d4): the movement guard retains the resolved injury type,
    # while free-text note body parts still cannot become extra injuries.
    assert context.training_context.injuries == ["hip flexor pain"]
    assert all("knee" not in injury for injury in context.training_context.injuries)


def test_runtime_context_transports_all_guided_injuries_and_keeps_legacy_first_card():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "left knee, right ankle"},
        ]
    )
    payload["guided_injuries"] = [
        {
            "area": "left knee",
            "injury_type": "instability",
            "injury_subtypes": ["pain", "instability", "tightness"],
            "severity": "moderate",
        },
        {"area": "right ankle", "injury_type": "sprain", "injury_subtypes": ["sprain"], "severity": "mild"},
    ]

    parsed = PlanInput.from_payload(payload)
    context = build_runtime_context(
        plan_input=parsed,
        random_seed=None,
        logger=logging.getLogger(__name__),
    )

    assert len(context.training_context.guided_injuries) == 2
    assert context.training_context.guided_injury == context.training_context.guided_injuries[0]
    assert context.training_context.guided_injury["injury_subtypes"] == ["pain", "instability", "tightness"]
    assert context.training_context.guided_injuries[0]["injury_subtypes"] == ["pain", "instability", "tightness"]
    assert context.training_context.guided_injuries[1]["injury_subtypes"] == ["sprain"]
    assert len(context.training_context.parsed_injuries) == 2
    assert context.training_context.parsed_injuries[0]["guided_source_injury_subtypes"] == ["pain", "instability", "tightness"]


def test_approved_resume_runtime_context_keeps_structured_injury_truth():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "hip flexor"},
        ]
    )
    payload["guided_injury"] = {
        "area": "hip flexor",
        "severity": "moderate",
        "trend": "stable",
        "avoid": "deep hip flexion",
        "notes": "pain at end-range",
    }

    parsed = PlanInput.from_payload(payload)
    context = build_runtime_context(
        plan_input=parsed,
        random_seed=None,
        logger=logging.getLogger(__name__),
        triage_summary={
            "mode": "needs_review",
            "should_block_stage2": True,
            "red_flags": ["instability"],
            "sparring_risk_band": "red",
        },
        is_approved_triage_resume=True,
    )

    assert context.training_context.injuries_raw_text == parsed.injuries
    assert context.training_context.parsed_injuries == [dict(entry) for entry in parsed.parsed_injuries]
    assert context.training_context.guided_injury is None
    assert context.training_context.guided_injuries == []
    assert context.training_context.injury_restrictions == [dict(entry) for entry in parsed.restrictions]
    assert context.training_context.triage_summary["mode"] == "full_plan"
    assert context.training_context.triage_summary["should_block_stage2"] is False
    assert context.training_context.triage_summary["triage_resume_approved"] is True
    assert context.training_context.triage_summary["red_flags"] == ["instability"]
    assert context.training_context.triage_summary["sparring_risk_band"] == "red"


def test_approved_resume_excludes_non_limiting_injury_from_generation_context():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "hip flexor"},
        ]
    )
    payload["guided_injury"] = {
        "area": "hip flexor",
        "severity": "low",
        "trend": "stable",
        "avoid": "",
        "notes": "mild tightness [training_impact:not_limiting]",
    }

    parsed = PlanInput.from_payload(payload)
    context = build_runtime_context(
        plan_input=parsed,
        random_seed=None,
        logger=logging.getLogger(__name__),
        is_approved_triage_resume=True,
    )

    assert parsed.parsed_injuries[0]["training_impact"] == "not_limiting"
    assert context.parsed_injury_phrases == []
    assert context.injuries_display == "None"
    assert context.injuries_only_text == ""
    assert context.training_context.injuries == []
    assert context.training_context.injuries_raw_text == ""
    assert context.training_context.parsed_injuries == []
    assert context.training_context.injury_restrictions == []
    # The intake itself is unchanged for persistence/Today, but generation only
    # reads the neutralized runtime context.
    assert parsed.parsed_injuries[0]["original_phrase"] == "hip flexor"


def test_guided_non_limiting_avoidance_without_an_area_does_not_crash():
    injuries, restrictions = input_parsing._parse_guided_injury(
        GuidedInjury(
            avoid="heavy overhead pressing",
            notes="minor discomfort [training_impact:not_limiting]",
        )
    )

    assert injuries == []
    assert restrictions
    assert restrictions[0]["training_impact"] == "not_limiting"


def test_full_plan_triage_excludes_non_limiting_injury_from_generation_context():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "hip flexor"},
        ]
    )
    payload["guided_injury"] = {
        "area": "hip flexor",
        "severity": "low",
        "trend": "stable",
        "avoid": "",
        "notes": "mild tightness [training_impact:not_limiting]",
    }

    context = build_runtime_context(
        plan_input=PlanInput.from_payload(payload),
        random_seed=None,
        logger=logging.getLogger(__name__),
        triage_summary={"mode": "full_plan", "should_block_stage2": False},
    )

    assert context.training_context.injuries == []
    assert context.training_context.parsed_injuries == []
    assert context.training_context.injury_restrictions == []
    assert context.training_context.guided_injury is None
    assert context.training_context.guided_injuries == []
    # Kept display-only so the plan still carries the injury note and the
    # re-injury stop rule.
    assert [
        entry.get("guided_area")
        for entry in context.training_context.non_limiting_injuries
    ] == ["hip flexor"]


def test_full_plan_triage_keeps_limiting_guided_cards():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "hip flexor, knee"},
        ]
    )
    payload["guided_injuries"] = [
        {"area": "hip flexor", "notes": "mild tightness [training_impact:not_limiting]"},
        {"area": "knee", "notes": "pain when squatting [training_impact:limiting]"},
    ]

    context = build_runtime_context(
        plan_input=PlanInput.from_payload(payload),
        random_seed=None,
        logger=logging.getLogger(__name__),
        triage_summary={"mode": "full_plan", "should_block_stage2": False},
    )

    assert context.training_context.guided_injury["area"] == "knee"
    assert [item["area"] for item in context.training_context.guided_injuries] == ["knee"]


def test_guided_injury_structural_notes_are_retained_in_original_phrase():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "right knee"},
        ]
    )
    payload["guided_injury"] = {
        "area": "right knee",
        "severity": "high",
        "trend": "stable",
        "notes": "rupture acl after a collision",
    }

    parsed = PlanInput.from_payload(payload)

    assert "rupture acl" in parsed.parsed_injuries[0]["original_phrase"].lower()


@pytest.mark.parametrize(
    ("guided_severity", "expected_severity"),
    [("low", "low"), ("high", "high")],
)
def test_guided_injury_payload_converts_frontend_to_backend_severity_vocab(guided_severity, expected_severity):
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "hip flexor"},
        ]
    )
    payload["guided_injury"] = {
        "area": "hip flexor",
        "severity": guided_severity,
    }

    parsed = PlanInput.from_payload(payload)

    assert parsed.parsed_injuries[0]["severity"] == expected_severity
    assert parsed.parsed_injuries[0]["severity_source"] == "guided_card"
    assert parsed.parsed_injuries[0]["severity_evidence"] == [f"guided severity: {expected_severity}"]


def test_parsed_injuries_never_store_mild_or_severe_internally():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "mild left shoulder soreness"},
        ]
    )
    parsed = PlanInput.from_payload(payload)

    assert all(entry["severity"] in {"low", "moderate", "high"} for entry in parsed.parsed_injuries)
    assert all(entry["severity"] not in {"mild", "severe"} for entry in parsed.parsed_injuries)


def test_guided_injury_legacy_payload_remains_compatible():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
            {"label": "Any injuries or areas you need to work around?", "value": "hip flexor"},
        ]
    )
    payload["guided_injury"] = {
        "area": "hip flexor",
        "severity": "moderate",
        "trend": "improving",
        "avoid": "deep hip flexion",
        "notes": "pain at end-range",
    }

    parsed = PlanInput.from_payload(payload)

    assert parsed.guided_injury is not None
    assert parsed.guided_injury.area == "hip flexor"
    assert parsed.guided_injury.severity == "moderate"
    assert parsed.guided_injury.avoid == "deep hip flexion"


def test_guided_injury_structured_payload_preserves_new_fields():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
        ]
    )
    payload["guided_injury"] = {
        "area": "left forearm",
        "injury_type": "abrasion",
        "surface_type": "mat burn",
        "timeframe": "48h",
        "cleared": True,
        "open_wound": False,
        "bleeding_status": "none",
        "infection_signs": ["redness", "warmth"],
        "impact_related": "yes",
        "sensitive_area": "no",
    }

    parsed = PlanInput.from_payload(payload)

    assert parsed.guided_injury is not None
    assert parsed.guided_injury.injury_type == "abrasion"
    assert parsed.guided_injury.surface_type == "mat burn"
    assert parsed.guided_injury.timeframe == "48h"
    assert parsed.guided_injury.cleared == "yes"
    assert parsed.guided_injury.open_wound == "no"
    assert parsed.guided_injury.bleeding_status == "none"
    assert parsed.guided_injury.infection_signs == ["redness", "warmth"]
    assert parsed.guided_injury.impact_related == "yes"
    assert parsed.guided_injury.sensitive_area == "no"


def test_guided_injury_infection_signs_defaults_to_empty_list():
    guided = input_parsing._extract_guided_injury({"guided_injury": {"area": "left forearm"}})

    assert guided is not None
    assert guided.infection_signs == []


def test_guided_injury_missing_structured_fields_do_not_crash():
    guided = input_parsing._extract_guided_injury(
        {"guided_injury": {"area": "left forearm", "unknown_field": "ignored"}}
    )

    assert guided is not None
    assert guided.injury_type == ""
    assert guided.open_wound == ""


def test_guided_injury_injury_subtypes_are_preserved():
    guided = input_parsing._extract_guided_injury(
        {"guided_injury": {"area": "left forearm", "injury_subtypes": ["sprain", "surface_injury:blister"]}}
    )

    assert guided is not None
    assert guided.injury_subtypes == ["sprain", "surface_injury:blister"]


def test_guided_injury_single_surface_subtype_promotes_resolution_type():
    payload = _payload(
        [
            {"label": "Full name", "value": "Test Athlete"},
            {"label": "Fighting Style (Technical)", "value": "Boxing"},
        ]
    )
    payload["guided_injury"] = {
        "area": "right heel",
        "injury_type": "pain",
        "injury_subtypes": ["surface_injury:blister"],
    }

    parsed = PlanInput.from_payload(payload)

    assert parsed.parsed_injuries
    assert parsed.parsed_injuries[0]["injury_type"] == "blister"


def test_missing_frequency_is_intentionally_inferred_and_marked_system_inferred():
    parsed = PlanInput.from_payload(
        _payload(
            [
                {"label": "Training Availability", "value": "Mon, Thu, Sat"},
            ]
        )
    )
    assert parsed.training_frequency == 3
    assert parsed.parsing_metadata["training_frequency"]["source"] == "system_inferred"


def test_user_supplied_frequency_is_marked_user_supplied():
    parsed = PlanInput.from_payload(
        _payload(
            [
                {"label": "Weekly Training Frequency", "value": "4"},
                {"label": "Training Availability", "value": "Mon, Wed, Thu, Sat"},
            ]
        )
    )
    assert parsed.training_frequency == 4
    assert parsed.parsing_metadata["training_frequency"]["source"] == "user_supplied"


def test_malformed_fight_date_raises_value_error():
    with pytest.raises(ValueError, match="invalid fight date format"):
        PlanInput.from_payload(
            _payload([{"label": "When is your next fight?", "value": "03-14-2026"}])
        )


def test_fight_date_parsers_keep_distinct_return_types():
    assert parse_fight_date("2026-03-14") == date(2026, 3, 14)
    parsed_datetime = input_parsing._parse_fight_datetime("2026-03-14")

    assert isinstance(parsed_datetime, datetime)
    assert parsed_datetime == datetime(2026, 3, 14)


@pytest.mark.parametrize(
    "fight_date_value",
    ["2026-03-14", "2026/03/14", "03/14/2026", "2026-03-14T00:00:00Z"],
)
def test_plan_input_accepts_supported_fight_date_formats(monkeypatch, fight_date_value):
    monkeypatch.setattr(input_parsing, "_utc_now", lambda: datetime(2026, 3, 1, 12, 0))

    parsed = PlanInput.from_payload(
        _payload(
            [
                {"label": "When is your next fight?", "value": fight_date_value},
                {"label": "Athlete Time Zone", "value": "UTC"},
            ]
        )
    )

    assert parsed.days_until_fight == 13
    assert parsed.weeks_out == 1


def test_include_generation_day_anchors_camp_one_day_earlier(monkeypatch):
    monkeypatch.setattr(input_parsing, "_utc_now", lambda: datetime(2026, 3, 1, 12, 0))
    fields = [
        {"label": "When is your next fight?", "value": "2026-03-14"},
        {"label": "Athlete Time Zone", "value": "UTC"},
    ]

    default = PlanInput.from_payload(_payload(fields))
    included = PlanInput.from_payload({**_payload(fields), "include_generation_day": True})

    # The camp calendar starts at D-(days_until_fight - 1); one extra day out
    # makes the generation day (D-13) the first plannable day.
    assert default.days_until_fight == 13
    assert default.include_generation_day is False
    assert included.days_until_fight == 14
    assert included.include_generation_day is True


def test_date_only_fight_date_with_missing_timezone_uses_platform_default_timezone(monkeypatch):
    monkeypatch.setattr(input_parsing, "_utc_now", lambda: datetime(2026, 3, 14, 0, 30))

    parsed = PlanInput.from_payload(
        _payload(
            [
                {"label": "Full name", "value": "West Coast Athlete"},
                {"label": "When is your next fight?", "value": "2026-03-14"},
            ]
        )
    )

    assert parsed.days_until_fight == 0
    assert parsed.weeks_out == 1
    assert parsed.parsing_metadata["athlete_timezone"]["source"] == "defaulted_missing"


def test_plan_input_uses_athlete_timezone_for_date_only_rollover(monkeypatch):
    monkeypatch.setattr(input_parsing, "_utc_now", lambda: datetime(2026, 3, 14, 0, 30))

    parsed = PlanInput.from_payload(
        _payload(
            [
                {"label": "Full name", "value": "West Coast Athlete"},
                {"label": "When is your next fight?", "value": "2026-03-14"},
                {"label": "Athlete Time Zone", "value": "UTC-08:00"},
                {"label": "Athlete Locale", "value": "en-US"},
            ]
        )
    )

    assert parsed.athlete_timezone == "UTC-08:00"
    assert parsed.athlete_locale == "en-US"
    assert parsed.days_until_fight == 1
    assert parsed.weeks_out == 1
    assert parsed.parsing_metadata["athlete_timezone"]["source"] == "user_supplied"


def test_invalid_athlete_timezone_falls_back_to_platform_default(monkeypatch):
    monkeypatch.setattr(input_parsing, "_utc_now", lambda: datetime(2026, 3, 14, 0, 30))

    parsed = PlanInput.from_payload(
        _payload(
            [
                {"label": "Full name", "value": "Fallback Athlete"},
                {"label": "When is your next fight?", "value": "2026-03-14"},
                {"label": "Timezone", "value": "Mars/Olympus"},
            ]
        )
    )

    assert parsed.athlete_timezone == "UTC"
    assert parsed.days_until_fight == 0
    assert parsed.weeks_out == 1
    assert parsed.parsing_metadata["athlete_timezone"]["source"] == "defaulted_missing"


def test_timestamped_and_date_only_countdown_use_consistent_date_model(monkeypatch):
    monkeypatch.setattr(input_parsing, "_utc_now", lambda: datetime(2026, 3, 13, 12, 0))

    date_only = PlanInput.from_payload(
        _payload(
            [
                {"label": "When is your next fight?", "value": "2026-03-15"},
                {"label": "Athlete Time Zone", "value": "UTC"},
            ]
        )
    )
    timestamped = PlanInput.from_payload(
        _payload(
            [
                {"label": "When is your next fight?", "value": "2026-03-15T00:00:00Z"},
                {"label": "Athlete Time Zone", "value": "UTC"},
            ]
        )
    )

    assert date_only.days_until_fight == 2
    assert timestamped.days_until_fight == 2


def test_countdown_depends_only_on_utc_now(monkeypatch):
    monkeypatch.setattr(input_parsing, "_utc_now", lambda: datetime(2026, 3, 13, 23, 30))

    parsed = PlanInput.from_payload(
        _payload([{"label": "When is your next fight?", "value": "2026-03-14"}])
    )

    assert parsed.days_until_fight == 1


def test_countdown_stable_across_timezone_edge_cases(monkeypatch):
    monkeypatch.setattr(input_parsing, "_utc_now", lambda: datetime(2026, 3, 14, 7, 30))

    parsed = PlanInput.from_payload(
        _payload(
            [
                {"label": "When is your next fight?", "value": "2026-03-14"},
                {"label": "Athlete Time Zone", "value": "UTC-08:00"},
            ]
        )
    )

    assert parsed.days_until_fight == 1


def _guided_payload(
    area: str,
    injury_type: str = "",
    surface_type: str = "",
    notes: str = "",
    avoid: str = "",
    injury_subtypes: list[str] | None = None,
) -> dict:
    payload = _payload([
        {"label": "Full name", "value": "Test Athlete"},
        {"label": "Fighting Style (Technical)", "value": "Boxing"},
    ])
    payload["guided_injury"] = {
        "area": area,
        "injury_type": injury_type,
        "surface_type": surface_type,
        "injury_subtypes": injury_subtypes or [],
        "notes": notes,
        "avoid": avoid,
    }
    return payload


def test_guided_resolver_parser_specific_type_beats_tendon_ligament_dropdown():
    parsed = PlanInput.from_payload(_guided_payload("Hyperextended right knee", injury_type="tendon_ligament"))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "hyperextension"
    assert entry["canonical_location"] == "knee"
    assert entry["side"] == "right"
    assert entry["injury_type_source"] == "parser"


def test_guided_resolver_fracture_dropdown_fills_unspecified_parse():
    parsed = PlanInput.from_payload(_guided_payload("right hand", injury_type="fracture"))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "fracture"
    assert entry["canonical_location"] == "hand"
    assert entry["injury_type_source"] == "guided_serious_type"


def test_guided_resolver_dislocation_dropdown_fills_unspecified_parse():
    parsed = PlanInput.from_payload(_guided_payload("right shoulder", injury_type="dislocation"))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "dislocation"
    assert entry["canonical_location"] == "shoulder"


def test_guided_resolver_surface_type_maps_to_surface_injury_type():
    parsed = PlanInput.from_payload(_guided_payload("right knee", injury_type="surface_injury", surface_type="bruise"))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "contusion"
    assert entry["canonical_location"] == "knee"
    assert entry["injury_type_source"] == "surface_type"


def test_guided_resolver_free_text_beats_subtype_and_preserves_subtypes():
    parsed = PlanInput.from_payload(
        _guided_payload("rolled right ankle with swelling", injury_type="pain", injury_subtypes=["pain"])
    )

    entry = parsed.parsed_injuries[0]
    # Free text beats the guided subtype, and the injury mechanism ("rolled")
    # wins over the swelling symptom: this resolves as a sprain.
    assert entry["injury_type"] == "sprain"
    assert entry["injury_type_source"] == "parser"
    assert entry["guided_source_injury_subtypes"] == ["pain"]


def test_guided_resolver_vague_text_allows_single_subtype_fallback():
    parsed = PlanInput.from_payload(_guided_payload("right ankle", injury_type="pain", injury_subtypes=["sprain"]))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "sprain"
    assert entry["injury_type_source"] == "guided_subtype"
    assert entry["guided_source_injury_subtypes"] == ["sprain"]


def test_guided_resolver_specific_guided_type_beats_vague_subtype():
    parsed = PlanInput.from_payload(_guided_payload("right ankle", injury_type="sprain", injury_subtypes=["pain"]))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "sprain"
    assert entry["injury_type_source"] == "guided_type"
    assert entry["guided_source_injury_subtypes"] == ["pain"]


def test_guided_resolver_multiple_subtypes_stay_metadata_only():
    parsed = PlanInput.from_payload(
        _guided_payload(
            "shoulder pain",
            injury_type="pain",
            injury_subtypes=["pain", "instability", "tightness"],
        )
    )

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "pain"
    assert entry["guided_source_injury_subtypes"] == ["pain", "instability", "tightness"]


def test_guided_resolver_unknown_subtype_never_becomes_final_type():
    parsed = PlanInput.from_payload(_guided_payload("right knee", injury_subtypes=["random_bad_token"]))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] != "random_bad_token"
    assert entry["guided_source_injury_subtypes"] == ["random_bad_token"]


def test_guided_resolver_surface_subtype_key_maps_to_supported_type():
    parsed = PlanInput.from_payload(
        _guided_payload("right heel", injury_type="pain", injury_subtypes=["surface_injury:blister"])
    )

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "blister"
    assert entry["injury_type_source"] == "guided_subtype"


def test_guided_resolver_skin_irritation_surface_type_maps_safely_to_abrasion():
    parsed = PlanInput.from_payload(
        _guided_payload("right forearm", injury_type="surface_injury", surface_type="skin_irritation")
    )

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "abrasion"


def test_guided_resolver_tendon_ligament_defaults_to_soft_tissue_without_rupture_evidence():
    parsed = PlanInput.from_payload(_guided_payload("right knee", injury_type="tendon_ligament"))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "soft_tissue_joint_issue"
    assert entry["injury_type"] != "tendon_rupture_or_avulsion"


def test_guided_resolver_tendon_ligament_uses_rupture_type_when_evidence_present():
    parsed = PlanInput.from_payload(_guided_payload("patellar tendon rupture", injury_type="tendon_ligament"))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] in {
        "tendon_rupture_or_avulsion",
        "patellar_tendon_rupture",
        "achilles_rupture",
        "quadriceps_tendon_rupture",
    }


def test_guided_resolver_unspecified_dropdown_keeps_parser_specific_parse():
    parsed = PlanInput.from_payload(_guided_payload("Rolled right ankle", injury_type="unspecified"))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "sprain"
    assert entry["canonical_location"] == "ankle"
    assert entry["side"] == "right"


def test_guided_resolver_empty_dropdown_falls_back_to_unspecified():
    parsed = PlanInput.from_payload(_guided_payload("right knee", injury_type=""))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "unspecified"
    assert entry["canonical_location"] == "knee"


def test_guided_resolver_notes_can_supply_specific_type_without_losing_area_location():
    parsed = PlanInput.from_payload(_guided_payload("right knee", injury_type="", notes="hyperextended during sparring"))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "hyperextension"
    assert entry["canonical_location"] == "knee"


def test_guided_display_location_skips_mechanism_phrases_for_hyperextension():
    parsed = PlanInput.from_payload(_guided_payload("Hyperextended right knee", injury_type="tendon_ligament"))
    entry = parsed.parsed_injuries[0]

    assert entry["injury_type"] == "hyperextension"
    assert entry["canonical_location"] == "knee"
    assert entry.get("display_location") in (None, "knee")
    assert format_injury_summary(entry) == "Right Knee — Hyperextension (Severity: Unspecified)"


def test_guided_display_location_skips_mechanism_phrases_for_sprain():
    parsed = PlanInput.from_payload(_guided_payload("Rolled left ankle", injury_type="unspecified"))
    entry = parsed.parsed_injuries[0]

    assert entry["injury_type"] == "sprain"
    assert entry["canonical_location"] == "ankle"
    summary = format_injury_summary(entry)
    assert summary.startswith("Left Ankle — Sprain")
    assert "Rolled" not in summary


def test_guided_display_location_keeps_clean_location_text():
    parsed = PlanInput.from_payload(_guided_payload("right hand", injury_type="fracture"))
    entry = parsed.parsed_injuries[0]

    assert entry["injury_type"] == "fracture"
    assert entry.get("display_location") == "hand"
    assert format_injury_summary(entry).startswith("Right Hand — Fracture")


def test_guided_display_location_skips_surface_injury_mechanism_phrase():
    parsed = PlanInput.from_payload(_guided_payload("right cheek cut", injury_type="surface_injury", surface_type="cut"))
    entry = parsed.parsed_injuries[0]
    summary = format_injury_summary(entry)

    assert "Right Right Cheek Cut" not in summary
    assert summary.startswith("Right ")
    assert " — Cut" in summary


def test_guided_display_location_skips_swollen_descriptor():
    parsed = PlanInput.from_payload(_guided_payload("Swollen left ankle", injury_type="unspecified"))
    summary = format_injury_summary(parsed.parsed_injuries[0])

    assert summary.startswith("Left Ankle")
    assert "Swollen Left Ankle" not in summary


def test_guided_display_location_skips_stiff_descriptor():
    parsed = PlanInput.from_payload(_guided_payload("Stiff right shoulder", injury_type="unspecified"))
    summary = format_injury_summary(parsed.parsed_injuries[0])

    assert summary.startswith("Right Shoulder")
    assert "Stiff Right Shoulder" not in summary


def test_guided_display_location_skips_tightness_descriptor():
    parsed = PlanInput.from_payload(_guided_payload("Tightness left hamstring", injury_type="unspecified"))
    summary = format_injury_summary(parsed.parsed_injuries[0])

    assert summary.startswith("Left Hamstring")
    assert "Tightness Left Hamstring" not in summary


def test_guided_display_location_skips_unstable_descriptor():
    parsed = PlanInput.from_payload(_guided_payload("Unstable right knee", injury_type="unspecified"))
    summary = format_injury_summary(parsed.parsed_injuries[0])

    assert summary.startswith("Right Knee")
    assert "Unstable Right Knee" not in summary


def test_guided_resolver_notes_instability_keeps_area_location_context():
    parsed = PlanInput.from_payload(_guided_payload("right knee", injury_type="", notes="felt like it gave way"))

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "instability"
    assert entry["canonical_location"] == "knee"


def test_guided_resolver_tendon_ligament_negated_rupture_does_not_escalate():
    parsed = PlanInput.from_payload(
        _guided_payload("right knee", injury_type="tendon_ligament", notes="no rupture or tear")
    )

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "soft_tissue_joint_issue"
    assert entry["injury_type"] != "tendon_rupture_or_avulsion"


def test_guided_resolver_tendon_ligament_red_flags_do_not_count_as_rupture_evidence():
    parsed = PlanInput.from_payload(
        _guided_payload(
            "right ankle",
            injury_type="tendon_ligament",
            notes="Cannot bear weight and unable to walk with obvious deformity.",
        )
    )

    entry = parsed.parsed_injuries[0]
    assert entry["injury_type"] == "soft_tissue_joint_issue"
    assert entry["injury_type"] != "tendon_rupture_or_avulsion"


def test_training_day_frequency_seven_valid_and_eight_rejected():
    days = "Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday"
    valid = _payload([
        {"label": "Training Days per Week", "value": "7"},
        {"label": "Training Availability", "value": days},
    ])
    assert PlanInput.from_payload(valid).training_frequency == 7
    invalid = _payload([
        {"label": "Training Days per Week", "value": "8"},
        {"label": "Training Availability", "value": days},
    ])
    with pytest.raises(ValueError, match="expected integer 1..7"):
        PlanInput.from_payload(invalid)
