"""Athlete exercise swaps, additions and removals on an app-generated plan."""
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from api.contracts.custom_plans import CustomDose
from api.models import ExerciseMedia


class ExerciseChangeScope(BaseModel):
    value: Literal["occurrence", "future"]
    label: str


class ExerciseAlternative(BaseModel):
    catalogue_id: str
    exercise_key: str
    name: str
    reason: str | None = None
    equipment: list[str] | None = None
    media: ExerciseMedia | None = None
    prescription: str | None = None
    progression_hold: bool | None = None
    bodyweight_progression: bool | None = None
    dose_required: bool = False
    dose_mode: Literal["reps", "duration", "intervals", "distance", "text"] = "reps"
    suggested_dose: dict[str, Any] | None = None


class ExerciseOptionsResponse(BaseModel):
    alternatives: list[ExerciseAlternative]
    more_available: bool = False
    notice: str | None = None
    scopes: list[ExerciseChangeScope]


class ExerciseChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: UUID
    action: Literal["swap", "add", "remove", "edit"]
    scope: Literal["occurrence", "future"] = "occurrence"
    date: str = Field(min_length=10, max_length=10)
    session_id: str = Field(min_length=1, max_length=200)
    block_id: str | None = Field(None, min_length=1, max_length=200)
    catalogue_id: str | None = Field(None, min_length=1, max_length=250)
    dose: CustomDose | None = None
    coaching_cues: list[str] | None = Field(None, max_length=10)
    stop_rules: list[str] | None = Field(None, max_length=10)
    regression_options: list[str] | None = Field(None, max_length=10)
    progression_rule: str | None = Field(None, max_length=500)
    confirmed: bool = False


class ExerciseUndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str | None = Field(None, min_length=10, max_length=10)


class ExerciseChangeResult(BaseModel):
    id: str
    action: Literal["swap", "add", "remove", "edit"]
    scope: Literal["occurrence", "future"]
    date: str
    name: str | None = None
    original_name: str | None = None
    revoked: bool = False
    # The occurrence's effective session after the change (StructuredSession
    # shape), so the card updates without a full plan reload.
    session: dict[str, Any] | None = None
