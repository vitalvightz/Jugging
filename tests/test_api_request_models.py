from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from api.models import (
    MANUAL_STAGE2_MAX_CHARS,
    GuidedInjuryInput,
    ManualStage2SubmissionRequest,
    NutritionSharedCampContext,
    PlanRenameRequest,
    PlanRequest,
)
from fightcamp.input_parsing import PlanInput
from fightcamp.plan_pipeline_runtime import build_runtime_context
from support import _build_request


def test_plan_request_to_payload_uses_existing_parser_labels():
    payload = _build_request().to_payload()
    labels = {field["label"] for field in payload["data"]["fields"]}

    assert "Full name" in labels
    assert "When is your next fight?" in labels
    assert "Training Availability" in labels
    assert "Athlete Time Zone" in labels
    assert "Training Days per Week" in labels


def test_plan_request_to_payload_keeps_list_backed_fields_as_lists_when_empty():
    payload = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": [],
        },
        fight_date="2099-04-18",
        equipment_access=[],
        training_availability=[],
        hard_sparring_days=[],
        support_work_days=[],
        key_goals=[],
        weak_areas=[],
    ).to_payload()

    fields = {field["label"]: field["value"] for field in payload["data"]["fields"]}

    assert fields["Equipment Access"] == []
    assert fields["Training Availability"] == []
    assert fields["Hard Sparring Days"] == []
    assert fields["Support Work Days"] == []
    assert fields["What are your key performance goals?"] == []
    assert fields["Primary goal"] == ""
    assert fields["Where do you feel weakest right now?"] == []
    assert fields["Primary weak area"] == ""


def test_plan_request_to_payload_includes_primary_goal_and_weak_area():
    payload = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": [],
        },
        fight_date="2099-04-18",
        key_goals=["power", "mobility"],
        primary_goal="power",
        weak_areas=["cns_fatigue", "hip_mobility"],
        primary_weak_area="hip_mobility",
    ).to_payload()

    fields = {field["label"]: field["value"] for field in payload["data"]["fields"]}
    assert fields["What are your key performance goals?"] == ["power", "mobility"]
    assert fields["Primary goal"] == "power"
    assert fields["Where do you feel weakest right now?"] == ["cns_fatigue", "hip_mobility"]
    assert fields["Primary weak area"] == "hip_mobility"


def test_plan_request_to_payload_includes_optional_collision_clarification():
    payload = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": [],
        },
        fight_date="2099-04-18",
        key_goals=["power"],
        primary_goal="power",
        weak_areas=["power"],
        primary_weak_area="power",
        goal_weakness_collision_tags=["power"],
        goal_weakness_collision_detail="Power drops when tired",
    ).to_payload()

    fields = {field["label"]: field["value"] for field in payload["data"]["fields"]}
    assert fields["Goal/weak-area collision tags"] == ["power"]
    assert fields["Goal/weak-area collision detail"] == "Power drops when tired"


def test_plan_request_collision_clarification_defaults_are_optional():
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": [],
        },
        fight_date="2099-04-18",
    )

    assert request.goal_weakness_collision_tags == []
    assert request.goal_weakness_collision_detail == ""


def test_plan_request_accepts_optional_intake_id_field():
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": [],
        },
        fight_date="2099-04-18",
        intake_id="intake-123",
    )

    assert request.intake_id == "intake-123"


def test_plan_request_rejects_more_than_four_hard_sparring_days():
    with pytest.raises(ValidationError, match="hard sparring days cap is 4"):
        PlanRequest(
            athlete={
                "full_name": "Ari Mensah",
                "technical_style": ["boxing"],
            },
            fight_date="2099-04-18",
            hard_sparring_days=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        )


def test_plan_request_rejects_hard_sparring_day_outside_training_availability():
    with pytest.raises(ValidationError, match="hard_sparring_days must be included in training_availability"):
        PlanRequest(
            athlete={
                "full_name": "Ari Mensah",
                "technical_style": ["boxing"],
            },
            fight_date="2099-04-18",
            training_availability=["Monday", "Wednesday"],
            hard_sparring_days=["Tuesday"],
        )


def test_plan_request_rejects_support_work_day_outside_training_availability():
    with pytest.raises(ValidationError, match="support_work_days must be included in training_availability"):
        PlanRequest(
            athlete={
                "full_name": "Ari Mensah",
                "technical_style": ["boxing"],
            },
            fight_date="2099-04-18",
            training_availability=["Monday", "Wednesday"],
            support_work_days=["Friday"],
        )


def test_plan_request_rejects_overlap_between_hard_sparring_and_support_work_days():
    with pytest.raises(ValidationError, match="hard_sparring_days and support_work_days must not overlap"):
        PlanRequest(
            athlete={
                "full_name": "Ari Mensah",
                "technical_style": ["boxing"],
            },
            fight_date="2099-04-18",
            training_availability=["Tuesday", "Thursday"],
            hard_sparring_days=["Tuesday"],
            support_work_days=["Tuesday"],
        )


def test_plan_request_keeps_strength_focus_with_hard_sparring_at_d20():
    """Intake never erases a selected goal; goal preservation records the deferral."""
    fight_date = (datetime.now(timezone.utc).date() + timedelta(days=20)).isoformat()

    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
        },
        fight_date=fight_date,
        training_availability=["Monday", "Tuesday"],
        hard_sparring_days=["Tuesday"],
        key_goals=["strength", "mobility"],
        primary_goal="strength",
        weak_areas=["strength", "footwork"],
        primary_weak_area="strength",
    )

    assert request.key_goals == ["strength", "mobility"]
    assert request.primary_goal == "strength"
    assert request.weak_areas == ["strength", "footwork"]
    assert request.primary_weak_area == "strength"


def test_plan_request_keeps_strength_focus_for_open_camp_with_hard_sparring():
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
        },
        fight_date=(datetime.now(timezone.utc).date() + timedelta(days=20)).isoformat(),
        no_scheduled_fight=True,
        training_availability=["Monday", "Tuesday"],
        hard_sparring_days=["Tuesday"],
        key_goals=["strength", "mobility"],
        primary_goal="strength",
        weak_areas=["strength", "footwork"],
        primary_weak_area="strength",
    )

    assert request.key_goals == ["strength", "mobility"]
    assert request.primary_goal == "strength"
    assert request.weak_areas == ["strength", "footwork"]
    assert request.primary_weak_area == "strength"


def test_plan_input_keeps_strength_focus_with_hard_sparring_at_d20_for_direct_payloads():
    fight_date = (datetime.now(timezone.utc).date() + timedelta(days=20)).isoformat()

    parsed = PlanInput.from_payload(
        {
            "data": {
                "fields": [
                    {"label": "Full name", "value": "Ari Mensah"},
                    {"label": "When is your next fight?", "value": fight_date},
                    {"label": "Athlete Time Zone", "value": "UTC"},
                    {"label": "Training Availability", "value": "Monday, Tuesday"},
                    {"label": "Hard Sparring Days", "value": "Tuesday"},
                    {"label": "What are your key performance goals?", "value": "strength, mobility"},
                    {"label": "Primary goal", "value": "strength"},
                    {"label": "Where do you feel weakest right now?", "value": "strength, footwork"},
                    {"label": "Primary weak area", "value": "strength"},
                ],
            },
            "no_scheduled_fight": False,
        }
    )

    assert parsed.key_goals == "strength, mobility"
    assert parsed.primary_goal == "strength"
    assert parsed.weak_areas == "strength, footwork"
    assert parsed.primary_weak_area == "strength"


def test_plan_input_keeps_strength_focus_for_direct_open_camp_payloads_with_hard_sparring():
    parsed = PlanInput.from_payload(
        {
            "data": {
                "fields": [
                    {"label": "Full name", "value": "Ari Mensah"},
                    {"label": "When is your next fight?", "value": ""},
                    {"label": "Training Availability", "value": "Monday, Tuesday"},
                    {"label": "Hard Sparring Days", "value": "Tuesday"},
                    {"label": "What are your key performance goals?", "value": "strength, mobility"},
                    {"label": "Primary goal", "value": "strength"},
                    {"label": "Where do you feel weakest right now?", "value": "strength, footwork"},
                    {"label": "Primary weak area", "value": "strength"},
                ],
            },
            "no_scheduled_fight": True,
        }
    )

    assert parsed.key_goals == "strength, mobility"
    assert parsed.primary_goal == "strength"
    assert parsed.weak_areas == "strength, footwork"
    assert parsed.primary_weak_area == "strength"


def test_plan_request_migrates_legacy_technical_skill_days_to_support_work_days():
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
        },
        fight_date="2099-04-18",
        training_availability=["Tuesday", "Friday"],
        technical_skill_days=["Tuesday", "Friday"],
    )

    assert request.support_work_days == ["Tuesday", "Friday"]


def test_plan_request_payload_round_trip_normalizes_real_app_goal_and_weakness_labels():
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": ["Counter Striker"],
        },
        fight_date="2026-08-30",
        training_availability=["Monday", "Tuesday", "Thursday", "Saturday"],
        hard_sparring_days=["Tuesday", "Saturday"],
        support_work_days=["Monday"],
        key_goals=["Power & Explosiveness", "Speed / Reaction"],
        weak_areas=["Coordination / Proprioception"],
    )

    parsed = PlanInput.from_payload(request.to_payload())
    context = build_runtime_context(
        plan_input=parsed,
        random_seed=1,
        logger=logging.getLogger(__name__),
    )

    assert context.training_context.key_goals == ["power", "speed"]
    assert context.training_context.weaknesses == ["coordination"]


def test_nutrition_shared_context_migrates_legacy_technical_skill_days_to_support_work_days():
    shared = NutritionSharedCampContext.model_validate(
        {
            "training_availability": ["Tuesday", "Friday"],
            "technical_skill_days": ["Tuesday", "Friday"],
        }
    )

    assert shared.support_work_days == ["Tuesday", "Friday"]


def test_plan_request_to_payload_includes_guided_injury_when_present():
    payload = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
        },
        fight_date="2099-04-18",
        injuries="hip flexor (moderate, improving). Avoid: deep hip flexion.",
        guided_injury={
            "area": "hip flexor",
            "severity": "moderate",
            "trend": "improving",
            "avoid": "deep hip flexion",
            "notes": "pain when driving knee up past pelvis",
        },
    ).to_payload()

    assert payload["guided_injury"]["area"] == "hip flexor"
    assert payload["guided_injury"]["avoid"] == "deep hip flexion"


def test_plan_request_to_payload_forwards_every_guided_injury_card():
    payload = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
        },
        fight_date="2099-04-18",
        injuries="hip flexor (moderate, improving). Avoid: deep hip flexion. Right heel. Notes: roadwork flare-up.",
        guided_injuries=[
            {
                "area": "hip flexor",
                "severity": "moderate",
                "trend": "improving",
                "avoid": "deep hip flexion",
            },
            {
                "area": "right heel",
                "notes": "roadwork flare-up",
            },
        ],
    ).to_payload()

    # Stage 1 consumes the plural list and parses every entry, so all cards must
    # be forwarded. The singular key mirrors the first entry for back-compat.
    assert [entry["area"] for entry in payload["guided_injuries"]] == [
        "hip flexor",
        "right heel",
    ]
    assert payload["guided_injury"]["area"] == "hip flexor"


def test_plan_request_to_payload_guided_injury_forwards_full_structured_contract():
    payload = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
        },
        fight_date="2099-04-18",
        guided_injuries=[
            {
                "area": "right knee",
                "severity": "moderate",
                "trend": "worsening",
                "avoid": "deep flexion and hard pivots",
                "notes": "hyperextension in sparring last week",
                "injury_type": "sprain",
                "injury_subtypes": ["mcl"],
                "surface_type": "",
                "timeframe": "last_week",
                "cleared": "no",
                "open_wound": "no",
                "bleeding_status": "none",
                "infection_signs": ["none"],
                "impact_related": "yes",
                "sensitive_area": "no",
            }
        ],
    ).to_payload()

    # The full structured guided-injury contract must reach Stage 1 / injury
    # triage; dropping any field silently downgrades classification and loses
    # medical-safety signals (open wound, bleeding, infection).
    expected_keys = {
        "area",
        "severity",
        "trend",
        "avoid",
        "notes",
        "injury_type",
        "injury_subtypes",
        "surface_type",
        "timeframe",
        "cleared",
        "open_wound",
        "bleeding_status",
        "infection_signs",
        "impact_related",
        "sensitive_area",
    }
    entry = payload["guided_injuries"][0]
    assert set(entry.keys()) == expected_keys
    assert entry["injury_type"] == "sprain"
    assert entry["injury_subtypes"] == ["mcl"]
    assert entry["bleeding_status"] == "none"
    assert entry["infection_signs"] == ["none"]
    # Singular mirror carries the same full contract for back-compat consumers.
    assert set(payload["guided_injury"].keys()) == expected_keys


def test_plan_request_payload_round_trip_into_plan_input():
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": ["pressure_fighter"],
        },
        fight_date="2026-08-30",
        training_availability=["Monday", "Tuesday", "Thursday", "Saturday"],
        hard_sparring_days=["Tuesday", "Saturday"],
        support_work_days=["Monday"],
        equipment_access=["barbell", "heavy_bag"],
        key_goals=["power", "conditioning"],
        weak_areas=["gas_tank", "defense"],
        guided_injury={
            "area": "left rib",
            "severity": "high",
            "trend": "worsening",
            "avoid": "contact and hard sparring",
            "notes": "pain breathing deeply after body shot",
        },
    )

    parsed = PlanInput.from_payload(request.to_payload())

    assert parsed.next_fight_date == "2026-08-30"
    assert parsed.training_days == ["Monday", "Tuesday", "Thursday", "Saturday"]
    assert parsed.hard_sparring_days == ["Tuesday", "Saturday"]
    assert parsed.support_work_days == ["Monday"]
    assert parsed.equipment_access == "barbell, heavy_bag"
    assert parsed.key_goals == "power, conditioning"
    assert parsed.weak_areas == "gas_tank, defense"
    assert parsed.guided_injury is not None
    assert parsed.guided_injury.area == "left rib"


@pytest.mark.parametrize(
    ("guided_severity", "expected_severity"),
    [("low", "low"), ("mild", "low"), ("moderate", "moderate"), ("severe", "high"), ("high", "high")],
)
def test_plan_request_guided_injury_severity_accepts_and_normalizes_aliases(guided_severity, expected_severity):
    payload = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
        },
        fight_date="2099-04-18",
        guided_injury={
            "area": "hip flexor",
            "severity": guided_severity,
        },
    ).to_payload()

    assert payload["guided_injury"]["severity"] == expected_severity


def test_plan_request_guided_injury_severity_rejects_unknown_values():
    with pytest.raises(ValidationError, match="guided injury severity must be one of low, moderate, or high"):
        PlanRequest(
            athlete={
                "full_name": "Ari Mensah",
                "technical_style": ["boxing"],
            },
            fight_date="2099-04-18",
            guided_injury={
                "area": "hip flexor",
                "severity": "critical",
            },
        )


def test_plan_request_coerces_fractional_height_values_for_saved_retries():
    req = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "height_cm": 182.8,
        },
        fight_date="2099-04-18",
    )
    req_from_string = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "height_cm": "182.2",
        },
        fight_date="2099-04-18",
    )

    assert req.athlete.height_cm == 183
    assert req_from_string.athlete.height_cm == 182


def test_plan_request_rejects_non_numeric_height_string():
    with pytest.raises(ValidationError, match="height_cm"):
        PlanRequest(
            athlete={
                "full_name": "Ari Mensah",
                "technical_style": ["boxing"],
                "height_cm": "six feet",
            },
            fight_date="2099-04-18",
        )


def test_record_format_validation_rejects_invalid_values():
    try:
        PlanRequest(
            athlete={
                "full_name": "Ari Mensah",
                "technical_style": ["boxing"],
                "record": "five and one",
            },
            fight_date="2099-04-18",
        )
    except Exception as exc:
        assert "x-x or x-x-x" in str(exc)
    else:
        raise AssertionError("invalid record format should be rejected")


def test_record_format_validation_accepts_valid_formats():
    for record in ("5-1", "12-2-1", "0-0", "10-0-3"):
        req = PlanRequest(
            athlete={
                "full_name": "Ari Mensah",
                "technical_style": ["boxing"],
                "record": record,
            },
            fight_date="2099-04-18",
        )
        assert req.athlete.record == record


def test_record_format_validation_accepts_empty_record():
    req = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "record": "",
        },
        fight_date="2099-04-18",
    )
    assert req.athlete.record == ""


def test_record_format_validation_rejects_partial_format():
    for bad in ("5-", "-1", "5", "5-1-2-3"):
        try:
            PlanRequest(
                athlete={
                    "full_name": "Ari Mensah",
                    "technical_style": ["boxing"],
                    "record": bad,
                },
                fight_date="2099-04-18",
            )
        except Exception as exc:
            assert "x-x or x-x-x" in str(exc)
        else:
            raise AssertionError(f"record '{bad}' should be rejected")


def test_plan_request_accepts_empty_fight_date_for_open_camp():
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": ["pressure_fighter"],
        },
        training_availability=["Monday", "Wednesday", "Friday"],
        weekly_training_frequency=3,
    )

    assert request.fight_date == ""

    parsed = PlanInput.from_payload(request.to_payload())

    # PlanInput must accept the empty date and downgrade camp-timeline fields.
    assert parsed.next_fight_date == ""
    assert parsed.days_until_fight is None
    assert parsed.weeks_out == "N/A"

    # The empty next_fight_date is no longer a generation blocker for open camps.
    assert "missing_next_fight_date" not in parsed.generation_issues()


def test_calculate_phase_weeks_falls_back_to_camp_length_when_fight_date_unknown():
    from fightcamp.camp_phases import GPP, SPP, TAPER, calculate_phase_weeks

    weeks = calculate_phase_weeks(
        camp_length=8,
        sport="boxing",
        days_until_fight=None,
    )

    # With no fight date, the function must fall back to camp_length * 7 days
    # and still produce a non-empty phase distribution summing to camp_length.
    phase_total = weeks[GPP] + weeks[SPP] + weeks[TAPER]
    assert phase_total == 8
    assert all(weeks[phase] >= 0 for phase in (GPP, SPP, TAPER))


# --- Explicit open-camp timeline coverage ----------------------------------


def test_plan_request_open_camp_flag_marks_plan_input_as_open_camp():
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": ["pressure_fighter"],
        },
        no_scheduled_fight=True,
        training_availability=["Monday", "Wednesday", "Friday"],
        weekly_training_frequency=3,
    )

    assert request.no_scheduled_fight is True
    assert request.fight_date == ""
    assert request.open_camp_weeks == 12

    payload = request.to_payload()
    assert payload["no_scheduled_fight"] is True
    assert payload["camp_timeline_type"] == "open_camp"
    assert payload["open_camp_weeks"] == 12

    parsed = PlanInput.from_payload(payload)
    assert parsed.no_scheduled_fight is True
    assert parsed.camp_timeline_type == "open_camp"
    assert parsed.next_fight_date == ""
    assert parsed.days_until_fight is None
    assert parsed.open_camp_weeks == 12
    assert "missing_next_fight_date" not in parsed.generation_issues()


def test_plan_request_open_camp_runtime_context_uses_camp_len_twelve():
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": ["pressure_fighter"],
        },
        no_scheduled_fight=True,
        training_availability=["Monday", "Wednesday", "Friday"],
        weekly_training_frequency=3,
        equipment_access=["bodyweight"],
    )

    parsed = PlanInput.from_payload(request.to_payload())
    context = build_runtime_context(
        plan_input=parsed,
        random_seed=1,
        logger=logging.getLogger(__name__),
    )

    assert context.camp_len == 12
    phase_total = sum(int(context.phase_weeks.get(p, 0)) for p in ("GPP", "SPP", "TAPER"))
    assert phase_total == 12


def test_plan_request_open_camp_honours_custom_open_camp_weeks():
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": ["pressure_fighter"],
        },
        no_scheduled_fight=True,
        open_camp_weeks=12,
        training_availability=["Monday", "Wednesday", "Friday"],
        weekly_training_frequency=3,
        equipment_access=["bodyweight"],
    )

    parsed = PlanInput.from_payload(request.to_payload())
    context = build_runtime_context(
        plan_input=parsed,
        random_seed=1,
        logger=logging.getLogger(__name__),
    )

    assert parsed.open_camp_weeks == 12
    assert context.camp_len == 12


def test_runtime_context_preserves_none_for_invalid_age_and_weight():
    payload = {
        "data": {
            "fields": [
                {"label": "Full name", "value": "Ari Mensah"},
                {"label": "Age", "value": "85kg"},
                {"label": "Weight", "value": "85,5"},
                {"label": "Fighting Style (Technical)", "value": ["boxing"]},
                {"label": "Training Availability", "value": ["Monday", "Wednesday", "Friday"]},
                {"label": "Sessions per Week", "value": 3},
                {"label": "When is your next fight?", "value": ""},
            ]
        },
        "no_scheduled_fight": True,
    }

    parsed = PlanInput.from_payload(payload)
    context = build_runtime_context(
        plan_input=parsed,
        random_seed=1,
        logger=logging.getLogger(__name__),
    )

    assert context.training_context.age is None
    assert context.training_context.weight is None


def test_runtime_context_parses_trimmed_numeric_age_and_weight():
    payload = {
        "data": {
            "fields": [
                {"label": "Full name", "value": "Ari Mensah"},
                {"label": "Age", "value": " 27 "},
                {"label": "Weight", "value": " 72.5 "},
                {"label": "Fighting Style (Technical)", "value": ["boxing"]},
                {"label": "Training Availability", "value": ["Monday", "Wednesday", "Friday"]},
                {"label": "Sessions per Week", "value": 3},
                {"label": "When is your next fight?", "value": ""},
            ]
        },
        "no_scheduled_fight": True,
    }

    parsed = PlanInput.from_payload(payload)
    context = build_runtime_context(
        plan_input=parsed,
        random_seed=1,
        logger=logging.getLogger(__name__),
    )

    assert context.training_context.age == 27
    assert context.training_context.weight == 72.5


def test_plan_input_blocks_scheduled_fight_without_date():
    # Explicit ``no_scheduled_fight=false`` plus empty ``fight_date`` is the
    # contract a future frontend would send when the user picked "scheduled
    # fight" but forgot to fill the date — block via generation_issues.
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": ["pressure_fighter"],
        },
        fight_date="",
        no_scheduled_fight=False,
        training_availability=["Monday", "Wednesday", "Friday"],
        weekly_training_frequency=3,
    )

    assert request.no_scheduled_fight is False

    parsed = PlanInput.from_payload(request.to_payload())
    assert parsed.camp_timeline_type == "scheduled_fight"
    assert "missing_next_fight_date" in parsed.generation_issues()


def test_plan_input_blocks_scheduled_fight_with_past_date():
    # A fight date before the generation day means ``days_until_fight`` is
    # clamped to ``None``, which would run the camp with ``weeks_out == "N/A"``
    # and break downstream phase logic — block via generation_issues.
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": ["pressure_fighter"],
        },
        fight_date="2020-05-30",
        no_scheduled_fight=False,
        training_availability=["Monday", "Wednesday", "Friday"],
        weekly_training_frequency=3,
    )

    parsed = PlanInput.from_payload(request.to_payload())
    assert parsed.camp_timeline_type == "scheduled_fight"
    assert parsed.days_until_fight is None
    assert "invalid_next_fight_date" in parsed.generation_issues()
    assert "missing_next_fight_date" not in parsed.generation_issues()


def test_plan_request_scheduled_fight_with_date_computes_countdown():
    request = PlanRequest(
        athlete={
            "full_name": "Ari Mensah",
            "technical_style": ["boxing"],
            "tactical_style": ["pressure_fighter"],
        },
        fight_date="2099-04-18",
        training_availability=["Monday", "Wednesday", "Friday"],
        weekly_training_frequency=3,
    )

    assert request.no_scheduled_fight is False

    parsed = PlanInput.from_payload(request.to_payload())
    assert parsed.camp_timeline_type == "scheduled_fight"
    assert parsed.next_fight_date == "2099-04-18"
    assert isinstance(parsed.days_until_fight, int) and parsed.days_until_fight > 0
    assert isinstance(parsed.weeks_out, int) and parsed.weeks_out >= 1
    assert "missing_next_fight_date" not in parsed.generation_issues()


def test_plan_input_back_compat_payload_without_no_scheduled_fight_flag():
    # Legacy payloads (PR #1263 shape) lack ``no_scheduled_fight`` entirely and
    # ship an empty ``fight_date``. They must continue to generate an open
    # camp without tripping ``missing_next_fight_date``.
    legacy_payload = {
        "data": {
            "fields": [
                {"label": "Full name", "value": "Ari Mensah"},
                {"label": "Fighting Style (Technical)", "value": ["boxing"]},
                {"label": "Training Availability", "value": ["Monday", "Wednesday", "Friday"]},
                {"label": "Sessions per Week", "value": 3},
                {"label": "When is your next fight?", "value": ""},
            ]
        },
    }

    parsed = PlanInput.from_payload(legacy_payload)
    assert parsed.no_scheduled_fight is True
    assert parsed.camp_timeline_type == "open_camp"
    assert parsed.next_fight_date == ""
    assert parsed.days_until_fight is None
    assert "missing_next_fight_date" not in parsed.generation_issues()


# --- Parser-consistency coverage for PlanInput.from_payload ----------------

def _open_camp_legacy_fields() -> list[dict]:
    return [
        {"label": "Full name", "value": "Ari Mensah"},
        {"label": "Fighting Style (Technical)", "value": ["boxing"]},
        {"label": "Training Availability", "value": ["Monday", "Wednesday", "Friday"]},
        {"label": "Sessions per Week", "value": 3},
        {"label": "When is your next fight?", "value": ""},
    ]


def test_plan_input_no_scheduled_fight_null_value_does_not_trigger_legacy_inference():
    # ``no_scheduled_fight: None`` is an explicit key (not absent), so the
    # legacy "infer from empty date" branch must NOT kick in.
    payload = {
        "data": {"fields": _open_camp_legacy_fields()},
        "no_scheduled_fight": None,
    }

    parsed = PlanInput.from_payload(payload)
    assert parsed.no_scheduled_fight is False
    assert parsed.camp_timeline_type == "scheduled_fight"
    # With an explicitly scheduled fight + empty date, generation must block.
    assert "missing_next_fight_date" in parsed.generation_issues()


@pytest.mark.parametrize("flag", ["true", "TRUE", "1", "yes", "y", "on"])
def test_plan_input_no_scheduled_fight_truthy_strings_parse_as_open_camp(flag: str):
    payload = {
        "data": {"fields": _open_camp_legacy_fields()},
        "no_scheduled_fight": flag,
    }

    parsed = PlanInput.from_payload(payload)
    assert parsed.no_scheduled_fight is True
    assert parsed.camp_timeline_type == "open_camp"


@pytest.mark.parametrize("flag", ["false", "FALSE", "0", "no", "n", "off"])
def test_plan_input_no_scheduled_fight_falsy_strings_parse_as_scheduled_fight(flag: str):
    payload = {
        "data": {"fields": _open_camp_legacy_fields()},
        "no_scheduled_fight": flag,
    }

    parsed = PlanInput.from_payload(payload)
    assert parsed.no_scheduled_fight is False
    assert parsed.camp_timeline_type == "scheduled_fight"


def test_plan_input_open_camp_weeks_numeric_string_rounds_to_int():
    payload = {
        "data": {"fields": _open_camp_legacy_fields()},
        "no_scheduled_fight": True,
        "open_camp_weeks": "8.0",
    }

    parsed = PlanInput.from_payload(payload)
    assert parsed.open_camp_weeks == 8


def test_plan_input_open_camp_weeks_float_rounds_not_truncates():
    payload = {
        "data": {"fields": _open_camp_legacy_fields()},
        "no_scheduled_fight": True,
        "open_camp_weeks": 12.9,
    }

    parsed = PlanInput.from_payload(payload)
    assert parsed.open_camp_weeks == 13


def test_plan_input_legacy_open_camp_weeks_zero_clamps_to_one():
    payload = {
        "data": {"fields": _open_camp_legacy_fields()},
        "no_scheduled_fight": True,
        "open_camp_weeks": 0,
    }

    parsed = PlanInput.from_payload(payload)
    assert parsed.open_camp_weeks == 1


def test_plan_input_open_camp_weeks_empty_string_falls_back_to_default():
    payload = {
        "data": {"fields": _open_camp_legacy_fields()},
        "no_scheduled_fight": True,
        "open_camp_weeks": "",
    }

    parsed = PlanInput.from_payload(payload)
    assert parsed.open_camp_weeks == 12


def test_plan_input_open_camp_weeks_invalid_string_raises_like_plan_request():
    payload = {
        "data": {"fields": _open_camp_legacy_fields()},
        "no_scheduled_fight": True,
        "open_camp_weeks": "banana",
    }

    with pytest.raises(ValueError, match="open_camp_weeks"):
        PlanInput.from_payload(payload)


def test_manual_stage2_submission_rejects_empty_text():
    with pytest.raises(ValidationError, match="final_plan_text is required"):
        ManualStage2SubmissionRequest(final_plan_text="   ")


def test_manual_stage2_submission_accepts_text_at_cap():
    request = ManualStage2SubmissionRequest(final_plan_text="x" * MANUAL_STAGE2_MAX_CHARS)
    assert len(request.final_plan_text) == MANUAL_STAGE2_MAX_CHARS


def test_manual_stage2_submission_rejects_oversize_text():
    with pytest.raises(ValidationError, match="at most"):
        ManualStage2SubmissionRequest(final_plan_text="x" * (MANUAL_STAGE2_MAX_CHARS + 1))


def test_guided_injury_input_enforces_field_caps():
    # Free-text fields accept generous input but reject abuse beyond the cap.
    GuidedInjuryInput(area="knee", notes="x" * 4000, avoid="y" * 2000)
    with pytest.raises(ValidationError):
        GuidedInjuryInput(notes="x" * 4001)
    with pytest.raises(ValidationError):
        GuidedInjuryInput(area="x" * 201)
    with pytest.raises(ValidationError):
        GuidedInjuryInput(injury_subtypes=["s"] * 65)
    with pytest.raises(ValidationError):
        GuidedInjuryInput(injury_subtypes=["s" * 65])
    with pytest.raises(ValidationError):
        GuidedInjuryInput(infection_signs=["s" * 65])


def _freq_request(value):
    return PlanRequest(
        athlete={"full_name": "Ari Mensah", "technical_style": ["boxing"]},
        fight_date="2099-04-18",
        weekly_training_frequency=value,
    )


def test_plan_rename_request_accepts_normal_name():
    assert PlanRenameRequest(plan_name="  Fight Camp 1  ").plan_name == "Fight Camp 1"


def test_plan_rename_request_rejects_empty_name():
    with pytest.raises(ValidationError, match="plan_name is required"):
        PlanRenameRequest(plan_name="   ")


def test_plan_rename_request_rejects_overlong_name():
    PlanRenameRequest(plan_name="x" * 120)
    with pytest.raises(ValidationError):
        PlanRenameRequest(plan_name="x" * 121)



def _open_weeks_request(value):
    return PlanRequest(
        athlete={"full_name": "Ari Mensah", "technical_style": ["boxing"]},
        no_scheduled_fight=True,
        open_camp_weeks=value,
        weekly_training_frequency=3,
        training_availability=["Monday", "Wednesday", "Friday"],
    )


def test_open_camp_weeks_accepts_bounds():
    assert _open_weeks_request(1).open_camp_weeks == 1
    assert _open_weeks_request(24).open_camp_weeks == 24
    assert _open_weeks_request("8.0").open_camp_weeks == 8


def test_open_camp_weeks_rejects_out_of_range_instead_of_clamping():
    for bad in (0, 25, 999, -3):
        with pytest.raises(ValidationError, match="between 1 and 24"):
            _open_weeks_request(bad)


def test_open_camp_weeks_rejects_non_numeric():
    with pytest.raises(ValidationError, match="must be numeric"):
        _open_weeks_request("banana")


def test_weekly_training_frequency_accepts_bounds():
    assert _freq_request(1).weekly_training_frequency == 1
    assert _freq_request(7).weekly_training_frequency == 7
    assert _freq_request("7.0").weekly_training_frequency == 7
    assert _freq_request(None).weekly_training_frequency is None


def test_weekly_training_frequency_rejects_out_of_range_instead_of_clamping():
    for bad in (0, 8, 10, 14, 999, -3):
        with pytest.raises(ValidationError, match="between 1 and 7"):
            _freq_request(bad)


@pytest.mark.parametrize("bad", [5.4, "5.4", 7.5, "2.2"])
def test_weekly_training_frequency_rejects_fractional_values(bad):
    with pytest.raises(ValidationError, match="whole number"):
        _freq_request(bad)


def test_weekly_training_frequency_rejects_non_numeric():
    with pytest.raises(ValidationError, match="must be numeric"):
        _freq_request("lots")


def test_plan_request_accepts_normal_profile_text_and_normalizes_blank_optional_text():
    request = PlanRequest(
        athlete={"full_name": " Ari Mensah ", "technical_style": [" boxing "]},
        fight_date="2099-04-18",
        injuries="   ",
        training_preference="  Short pads first.  ",
        mindset_challenges="   ",
        notes="  Loved reactive defence. ",
    )

    assert request.athlete.full_name == "Ari Mensah"
    assert request.injuries == ""
    assert request.training_preference == "Short pads first."
    assert request.mindset_challenges == ""
    assert request.notes == "Loved reactive defence."


def test_plan_request_rejects_overlong_athlete_full_name():
    with pytest.raises(ValidationError) as exc_info:
        PlanRequest(athlete={"full_name": "A" * 121, "technical_style": ["boxing"]})

    assert "athlete.full_name" in str(exc_info.value) or "full_name" in str(exc_info.value)


def test_plan_request_rejects_overlong_injuries_and_free_text_notes():
    with pytest.raises(ValidationError) as injuries_exc:
        PlanRequest(athlete={"full_name": "Ari Mensah"}, injuries="x" * 2001)
    assert "injuries" in str(injuries_exc.value)

    with pytest.raises(ValidationError) as notes_exc:
        PlanRequest(athlete={"full_name": "Ari Mensah"}, notes="x" * 1501)
    assert "notes" in str(notes_exc.value)


def test_plan_request_rejects_overlong_list_item_and_excessive_profile_list_length():
    with pytest.raises(ValidationError) as item_exc:
        PlanRequest(athlete={"full_name": "Ari Mensah"}, key_goals=["x" * 121])
    assert "key_goals" in str(item_exc.value)

    with pytest.raises(ValidationError) as length_exc:
        PlanRequest(
            athlete={
                "full_name": "Ari Mensah",
                "technical_style": [f"style-{idx}" for idx in range(33)],
            }
        )
    assert "technical_style" in str(length_exc.value)


def test_collision_detail_key_and_value_errors_use_specific_limits():
    with pytest.raises(ValidationError) as key_exc:
        PlanRequest(
            athlete={"full_name": "Ari Mensah"},
            goal_weakness_collision_details=[{"k" * 121: "ok"}],
        )
    assert "keys must be at most 120 characters" in str(key_exc.value)

    with pytest.raises(ValidationError) as value_exc:
        PlanRequest(
            athlete={"full_name": "Ari Mensah"},
            goal_weakness_collision_details=[{"power": "x" * 1001}],
        )
    assert "values must be at most 1000 characters" in str(value_exc.value)
