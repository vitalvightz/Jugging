"""Read-only personal movement summaries and historical exercise-block results."""
from typing import Literal

from pydantic import BaseModel, Field

from api.contracts.manual_exercises import MeasurementFormat
from api.contracts.session_details import SessionDetailBlock
from api.models import ExerciseLogActual, ExerciseLogRecord


class RecordedExerciseLoad(BaseModel):
    value: float
    unit: Literal["kg", "lb"]
    training_day: str
    basis: Literal["reported", "confirmed_prescription"]


class PersonalExercise(BaseModel):
    id: str
    name: str
    # canonical: an exact catalogue key; personal: a private movement the athlete
    # named; legacy: one unkeyed prescribed log, never merged with others.
    identity_kind: Literal["canonical", "legacy", "personal"]
    latest_day: str
    latest_values: ExerciseLogActual
    latest_confirmed_as_prescribed: bool
    best_loads: list[RecordedExerciseLoad] = Field(default_factory=list)
    # Where the latest record came from: a prescribed log or a manual session.
    latest_source: Literal["plan", "manual"] = "plan"
    # The measurement layout to offer when this movement is logged again.
    log_format: MeasurementFormat | None = None
    # Raw reported evidence and a current bank verdict, not reconstructed targets.
    latest_performance: ExerciseLogRecord | None = None
    progression_hold: bool | None = None
    bodyweight_progression: bool | None = None
    allow_progression: bool = False


class ExerciseLibraryResponse(BaseModel):
    exercises: list[PersonalExercise]
    as_of: str
    coverage: Literal["retained_logs"] = "retained_logs"


class ExercisePerformance(BaseModel):
    id: str
    training_day: str
    block: SessionDetailBlock
    # Same eligibility and frozen-prescription resolution as the library index.
    # Null means the record cannot supply chart evidence.
    performed_values: ExerciseLogActual | None = None


class ExercisePerformancesResponse(BaseModel):
    performances: list[ExercisePerformance]
    next_cursor: str | None = None
