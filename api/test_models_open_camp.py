import pytest
from pydantic import ValidationError

from api.models import (
    MAX_OPEN_CAMP_WEEKS,
    AthleteProfileInput,
    GuidedInjuryInput,
    PlanRequest,
)


def _athlete() -> AthleteProfileInput:
    return AthleteProfileInput(
        full_name="Test Athlete",
        technical_style=["boxing"],
        tactical_style=[],
        stance="orthodox",
        professional_status="amateur",
        athlete_timezone="UTC",
    )


def test_to_payload_open_camp_clears_fight_date_field() -> None:
    request = PlanRequest(
        athlete=_athlete(),
        fight_date="2026-12-01",
        no_scheduled_fight=True,
        rounds_format="3 x 3",
        weekly_training_frequency=4,
    )

    payload = request.to_payload()
    fields = payload["data"]["fields"]
    fight_date_field = next(field for field in fields if field["label"] == "When is your next fight?")

    assert fight_date_field["value"] == ""
    assert payload["camp_timeline_type"] == "open_camp"
    assert payload["no_scheduled_fight"] is True


def test_to_payload_forwards_all_guided_injuries() -> None:
    request = PlanRequest(
        athlete=_athlete(),
        fight_date="2026-12-01",
        rounds_format="3 x 3",
        weekly_training_frequency=4,
        guided_injuries=[
            GuidedInjuryInput(area="left knee", severity="moderate"),
            GuidedInjuryInput(area="right shoulder", severity="high"),
            GuidedInjuryInput(area="concussion", severity="high"),
        ],
    )

    payload = request.to_payload()

    # Every guided injury must reach Stage 1 via the plural key it consumes.
    assert [entry["area"] for entry in payload["guided_injuries"]] == [
        "left knee",
        "right shoulder",
        "concussion",
    ]
    # Singular key retained for back-compat, mirrors the first entry.
    assert payload["guided_injury"]["area"] == "left knee"


def test_to_payload_prioritizes_plural_guided_injuries_when_both_present() -> None:
    # The frontend always submits both fields (guided_injury mirrors the first
    # of guided_injuries), so the plural list must win or extra injuries are
    # dropped before they reach Stage 1.
    request = PlanRequest(
        athlete=_athlete(),
        guided_injury=GuidedInjuryInput(area="legacy knee", severity="low"),
        guided_injuries=[
            GuidedInjuryInput(area="left knee", severity="moderate"),
            GuidedInjuryInput(area="right shoulder", severity="high"),
            GuidedInjuryInput(area="concussion", severity="high"),
        ],
    )

    payload = request.to_payload()

    assert [entry["area"] for entry in payload["guided_injuries"]] == [
        "left knee",
        "right shoulder",
        "concussion",
    ]
    assert payload["guided_injury"]["area"] == "left knee"


def test_to_payload_singular_guided_injury_unchanged() -> None:
    request = PlanRequest(
        athlete=_athlete(),
        guided_injury=GuidedInjuryInput(area="left knee", severity="moderate"),
    )

    payload = request.to_payload()

    assert payload["guided_injury"]["area"] == "left knee"
    assert "guided_injuries" not in payload


@pytest.mark.parametrize(
    "raw, expected",
    [
        (MAX_OPEN_CAMP_WEEKS, MAX_OPEN_CAMP_WEEKS),
        ("8.0", 8),
        (8, 8),
        (1, 1),
    ],
)
def test_open_camp_weeks_accepts_in_range(raw: object, expected: int) -> None:
    request = PlanRequest(athlete=_athlete(), open_camp_weeks=raw)
    assert request.open_camp_weeks == expected


@pytest.mark.parametrize("raw", [999, MAX_OPEN_CAMP_WEEKS + 1, 0, -5, "9999"])
def test_open_camp_weeks_rejects_out_of_range(raw: object) -> None:
    # Out-of-range values are rejected with a clean 422 rather than silently
    # clamped, so malformed payloads surface instead of being masked.
    with pytest.raises(ValidationError, match=f"between 1 and {MAX_OPEN_CAMP_WEEKS}"):
        PlanRequest(athlete=_athlete(), open_camp_weeks=raw)


@pytest.mark.parametrize("days", [1, 7])
def test_training_days_per_week_accepts_one_through_seven(days: int) -> None:
    assert PlanRequest(athlete=_athlete(), weekly_training_frequency=days).weekly_training_frequency == days


@pytest.mark.parametrize("days", [0, 8, 10])
def test_training_days_per_week_rejects_out_of_range(days: int) -> None:
    with pytest.raises(ValidationError, match="between 1 and 7"):
        PlanRequest(athlete=_athlete(), weekly_training_frequency=days)


def test_training_days_per_week_cannot_exceed_availability() -> None:
    with pytest.raises(ValidationError, match="cannot exceed selected training_availability days"):
        PlanRequest(athlete=_athlete(), weekly_training_frequency=3, training_availability=["Monday", "Wednesday"])


@pytest.mark.parametrize("value", [1, 7, "7.0", None])
def test_nutrition_training_days_accepts_valid_values(value: object) -> None:
    from api.models import NutritionSharedCampContext
    expected = None if value is None else int(float(value))
    assert NutritionSharedCampContext(weekly_training_frequency=value).weekly_training_frequency == expected


@pytest.mark.parametrize("value", [0, 8, 10, 14])
def test_nutrition_training_days_rejects_legacy_session_counts(value: int) -> None:
    from api.models import NutritionSharedCampContext
    with pytest.raises(ValidationError, match="between 1 and 7"):
        NutritionSharedCampContext(weekly_training_frequency=value)


@pytest.mark.parametrize("value", [5.4, "5.4"])
def test_nutrition_training_days_rejects_fractions(value: object) -> None:
    from api.models import NutritionSharedCampContext
    with pytest.raises(ValidationError, match="whole number"):
        NutritionSharedCampContext(weekly_training_frequency=value)
