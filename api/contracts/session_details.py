"""Small, read-only projection of one historical training occurrence."""
from typing import Any, Literal

from pydantic import BaseModel, Field

from api.contracts.external_training import DistanceUnit, RunType, TrainingHistorySession
from api.contracts.manual_exercises import MeasurementFormat
from api.contracts.rehab_exposure import ExposureDose, ExposureResponse
from api.models import ExerciseLogActual


class SessionDetailBlock(BaseModel):
    name: str
    kind: Literal["exercise", "rehab", "guidance"] = "exercise"
    # manual: an exercise the athlete recorded on their own manual session. It
    # has no prescription, so nothing compares it against one.
    source: Literal["plan", "manual"] = "plan"
    format: MeasurementFormat | None = None
    prescribed: dict[str, Any] = Field(default_factory=dict)
    actual: ExerciseLogActual = Field(default_factory=ExerciseLogActual)
    status: Literal["as_prescribed", "modified", "skipped", "done_as_shown", "changed", "stopped"] | None = None
    reason: str | None = None
    notes: str | None = None
    completed_dose: ExposureDose | None = None
    responses: list[ExposureResponse] = Field(default_factory=list)


class SessionDetailSparring(BaseModel):
    intensity: str
    rounds_completed: int
    round_seconds: int | None = None
    head_contact: str | None = None
    rocked: bool | None = None
    notes: str | None = None


class SessionDetailRun(BaseModel):
    """One of the athlete's logged runs, enough to compare a run against."""
    id: str
    training_day: str
    created_at: str | None = None
    duration_minutes: int
    duration_seconds: int | None = None
    distance_value: float | None = None
    distance_unit: DistanceUnit | None = None
    run_type: RunType | None = None


class SessionDetails(BaseModel):
    session: TrainingHistorySession
    blocks: list[SessionDetailBlock] = Field(default_factory=list)
    prescription_source: Literal["snapshot", "exercise_logs", "unavailable", "manual"]
    message: str | None = None
    sparring: SessionDetailSparring | None = None
    # A run only: every live run the athlete logged through the end of its
    # week, so bests and comparisons never rest on a partly loaded History.
    runs: list[SessionDetailRun] | None = None
