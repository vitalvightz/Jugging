"""Saved self-directed drafts. These contracts never authorise execution."""

from datetime import datetime
from typing import Annotated, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from api.contracts.training_library import LibraryEntry

Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
Weekday = Literal[
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
]
WEEKDAYS = get_args(Weekday)


def ordered_sessions(sessions):
    # Python's stable sort retains athlete order within a day and among drafts.
    return sorted(sessions, key=lambda session: WEEKDAYS.index(session.weekday) if session.weekday else 7)


class DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomDose(DraftModel):
    mode: Literal["reps", "duration", "intervals", "distance", "text"] = "reps"
    sets: int | None = Field(None, strict=True, ge=1, le=100)
    reps: int | None = Field(None, strict=True, ge=1, le=1000)
    duration_seconds: int | None = Field(None, strict=True, ge=1, le=36000)
    rest_seconds: int | None = Field(None, strict=True, ge=0, le=3600)
    load_or_intensity: str | None = Field(None, max_length=80)

    rounds: int | None = Field(None, strict=True, ge=1, le=100)
    work_seconds: int | None = Field(None, strict=True, ge=1, le=36000)
    rpe: float | None = Field(None, strict=True, ge=1, le=10)
    tempo: str | None = Field(None, max_length=40)
    per_side: bool | None = Field(None, strict=True)
    distance: float | None = Field(None, strict=True, gt=0, le=100000)
    distance_unit: Literal["m", "km", "mi", "yd"] | None = None
    prescription: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def matching_mode(self):
        fields = {
            "reps": (
                "duration_seconds",
                "rounds",
                "work_seconds",
                "distance",
                "distance_unit",
                "prescription",
            ),
            "duration": ("reps", "rounds", "work_seconds", "prescription"),
            "intervals": (
                "sets",
                "reps",
                "duration_seconds",
                "distance",
                "distance_unit",
                "prescription",
            ),
            "distance": ("reps", "rounds", "work_seconds", "duration_seconds", "prescription"),
            "text": (
                "sets",
                "reps",
                "duration_seconds",
                "rounds",
                "work_seconds",
                "distance",
                "distance_unit",
            ),
        }
        if any(getattr(self, field) is not None for field in fields[self.mode]):
            raise ValueError("Fields do not match the selected dose mode.")
        return self


class CustomExerciseInput(DraftModel):
    id: UUID
    origin: Literal["catalogue"] = "catalogue"
    catalogue_id: str = Field(min_length=1, max_length=250)
    dose: CustomDose = Field(default_factory=CustomDose)
    coaching_cues: list[Annotated[str, StringConstraints(max_length=500)]] | None = Field(None, max_length=10)
    stop_rules: list[Annotated[str, StringConstraints(max_length=500)]] | None = Field(None, max_length=10)


class ManualExerciseInput(DraftModel):
    id: UUID
    origin: Literal["manual"]
    exercise_type: Literal["strength", "bodyweight", "power", "other"] | None = None
    name: Name
    equipment: str | None = Field(None, max_length=120)
    notes: str | None = Field(None, max_length=2000)
    media_id: UUID | None = None
    community_id: UUID | None = None
    community_revision_id: UUID | None = None
    dose: CustomDose = Field(default_factory=CustomDose)
    coaching_cues: list[Annotated[str, StringConstraints(max_length=500)]] | None = Field(None, max_length=10)
    stop_rules: list[Annotated[str, StringConstraints(max_length=500)]] | None = Field(None, max_length=10)


ExerciseInput = Annotated[
    CustomExerciseInput | ManualExerciseInput, Field(discriminator="origin")
]


def legacy_origins(value):
    if isinstance(value, dict):
        value = dict(value)
        value["exercises"] = [
            dict(exercise, origin=exercise.get("origin", "catalogue"))
            for exercise in value.get("exercises", [])
        ]
    return value


class CustomSessionInput(DraftModel):
    id: UUID
    name: Name
    weekday: Weekday | None = None
    session_type: Literal["mixed", "strength_power", "conditioning", "skill", "sparring", "recovery", "rest"] = "mixed"
    exercises: list[ExerciseInput] = Field(default_factory=list, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def legacy(cls, value):
        return legacy_origins(value)


class CustomPlanInput(DraftModel):
    name: Name
    sessions: list[CustomSessionInput] = Field(default_factory=list, max_length=28)

    @model_validator(mode="after")
    def unique_occurrences(self):
        ids = [session.id for session in self.sessions]
        ids += [
            exercise.id for session in self.sessions for exercise in session.exercises
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("Session and exercise IDs must be unique within a plan.")
        self.sessions = ordered_sessions(self.sessions)
        return self


class CustomExerciseSnapshot(LibraryEntry):
    category: Literal["training"] = "training"
    source: str
    source_revision: str
    row_revision: str


class CustomExercise(CustomExerciseInput):
    snapshot: CustomExerciseSnapshot
    incomplete: bool


class ManualExercise(ManualExerciseInput):
    incomplete: bool


class CustomSession(CustomSessionInput):
    exercises: list[
        Annotated[CustomExercise | ManualExercise, Field(discriminator="origin")]
    ] = Field(default_factory=list)


class CustomPlan(BaseModel):
    id: UUID
    name: str
    kind: Literal["self_directed"] = "self_directed"
    sessions: list[CustomSession]
    incomplete: bool
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def calendar_order(self):
        # Existing saved plans gain the same display order without rewriting IDs.
        self.sessions = ordered_sessions(self.sessions)
        return self


class CustomDoseSuggestion(BaseModel):
    dose: CustomDose | None = None
    format_dose: CustomDose | None = None
    original: str
    source: str
    source_revision: str
    row_revision: str
