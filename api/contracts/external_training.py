"""Completed external work, independent of plan/prescription identities."""
from datetime import date
from math import ceil
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from api.contracts.training_disciplines import SessionType, Sport, discipline_error

Activity = Literal["sparring", "boxing", "roadwork", "strength", "conditioning", "other"]
Effort = Literal["easy", "moderate", "hard"]
DistanceUnit = Literal["km", "miles"]
# Same caps as the manual exercise distance limits.
DISTANCE_LIMITS: dict[str, float] = {"km": 200, "miles": 125}


class ExternalTrainingFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    activity: Activity
    # Same upper bound as the existing session_logs duration contract.
    duration_minutes: StrictInt = Field(ge=1, le=600)
    effort: Effort
    # Optional detail on what was trained. Absent on older records and never
    # inferred for them; ``activity`` must be the kind the pair maps to.
    sport: Sport | None = None
    session_type: SessionType | None = None
    # Optional run detail (roadwork only). ``duration_minutes`` stays the
    # workload value; an exact time must round up to it.
    distance_value: float | None = Field(default=None, gt=0)
    distance_unit: DistanceUnit | None = None
    duration_seconds: StrictInt | None = Field(default=None, ge=1, le=36000)

    @model_validator(mode="after")
    def check_discipline(self):
        if error := discipline_error(self.activity, self.sport, self.session_type):
            raise ValueError(error)
        return self

    @model_validator(mode="after")
    def check_run_detail(self):
        if (self.distance_value is None) != (self.distance_unit is None):
            raise ValueError("Distance needs both a value and a unit.")
        if self.distance_value is not None:
            if self.activity != "roadwork":
                raise ValueError("Distance can only be logged for roadwork.")
            if self.distance_value > DISTANCE_LIMITS[self.distance_unit or "km"]:
                raise ValueError(f"Distance cannot exceed {DISTANCE_LIMITS[self.distance_unit or 'km']:g} {self.distance_unit}.")
            self.distance_value = round(self.distance_value, 2)
            if self.distance_value <= 0:
                raise ValueError("Distance must be at least 0.01.")
        if self.duration_seconds is not None and self.activity != "roadwork":
            raise ValueError("An exact time can only be logged for roadwork.")
        if self.duration_seconds is not None and self.duration_minutes != ceil(self.duration_seconds / 60):
            raise ValueError("duration_minutes must be the time rounded up to whole minutes.")
        return self


class ExternalTrainingCreate(ExternalTrainingFields):
    client_request_id: UUID
    training_day: date | None = None

    @field_validator("training_day", mode="before")
    @classmethod
    def validate_training_day(cls, value):
        if value is None:
            return None
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("Use a training date in YYYY-MM-DD format.")
        return date.fromisoformat(value)


class TrainingHistorySession(BaseModel):
    completion_origin: Literal["reported", "day_credit"] | None = None
    id: str
    source: Literal["unlxck", "external"]
    training_day: str
    status: Literal["done", "modified", "skipped"]
    title: str
    activity: Activity | None = None
    sport: Sport | None = None
    session_type: SessionType | None = None
    duration_minutes: int | None = None
    distance_value: float | None = None
    distance_unit: DistanceUnit | None = None
    duration_seconds: int | None = None
    effort: Effort | None = None
    plan_id: str | None = None
    session_rpe: int | None = None
    pain_after: int | None = None
    modification_reason: str | None = None
    notes: str | None = None
    # A manual session's short note on what was learned; never workload.
    takeaway: str = ""
    # Live exercise entries on a manual session; None where not counted.
    exercise_count: int | None = None
    # A plan session History may edit (RPE, pain, notes) or remove.
    history_editable: bool = False


class TrainingHistoryResponse(BaseModel):
    sessions: list[TrainingHistorySession]
    # Pass as ``before_day`` for the next, older page; None when there is none.
    next_before_day: str | None = None


TAKEAWAY_MAX_CHARS = 280


class ExternalTrainingTakeaway(BaseModel):
    model_config = ConfigDict(extra="forbid")
    takeaway: str = Field(default="", max_length=TAKEAWAY_MAX_CHARS)

    @field_validator("takeaway", mode="before")
    @classmethod
    def clean_takeaway(cls, value):
        return value.strip() if isinstance(value, str) else value
