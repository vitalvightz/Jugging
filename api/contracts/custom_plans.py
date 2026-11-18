"""Saved self-directed drafts. These contracts never authorise execution."""

from datetime import date, datetime, timedelta
from typing import Annotated, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from api.contracts.training_library import LibraryEntry
from api.structured_plan_models import ExercisePrescription

Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
Weekday = Literal[
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
]
WEEKDAYS = get_args(Weekday)


def ordered_sessions(sessions):
    # Python's stable sort retains athlete order within a day and among drafts.
    return sorted(sessions, key=lambda session: (str(getattr(session, "scheduled_date", None) or ""), WEEKDAYS.index(session.weekday) if session.weekday else 7))


class DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomDose(DraftModel):
    mode: Literal["reps", "duration", "intervals", "distance", "text", "stages"] = "reps"
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

    prescribed_sets: list[ExercisePrescription] | None = Field(None, max_length=100, json_schema_extra={"x-response-optional": True})

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
        fields["stages"] = (*fields["text"], "prescription")
        if self.mode != "stages" and self.prescribed_sets:
            raise ValueError("Loading stages require the stages dose mode.")
        if any(getattr(self, field) is not None for field in fields[self.mode]):
            raise ValueError("Fields do not match the selected dose mode.")
        return self


# What part of training an exercise is, when the session type doesn't say it
# (a sparring drill in a "Boxing + Conditioning" day). None: from the session.
ExerciseBlockType = Literal["preparation", "strength", "plyometric_power", "conditioning", "skill", "sparring", "cooldown_recovery"]


class CustomExerciseInput(DraftModel):
    exercise_code: str | None = Field(None, pattern=r"^[A-Za-z][1-9][0-9]*$", json_schema_extra={"x-response-optional": True})
    group_type: Literal["superset", "circuit", "alternating_sets"] | None = Field(None, json_schema_extra={"x-response-optional": True})
    id: UUID
    origin: Literal["catalogue"] = "catalogue"
    catalogue_id: str = Field(min_length=1, max_length=250)
    block_type: ExerciseBlockType | None = Field(None, json_schema_extra={"x-response-optional": True})
    dose: CustomDose = Field(default_factory=CustomDose)
    coaching_cues: list[Annotated[str, StringConstraints(max_length=500)]] | None = Field(None, max_length=10)
    stop_rules: list[Annotated[str, StringConstraints(max_length=500)]] | None = Field(None, max_length=10)


class ManualExerciseInput(DraftModel):
    exercise_code: str | None = Field(None, pattern=r"^[A-Za-z][1-9][0-9]*$", json_schema_extra={"x-response-optional": True})
    group_type: Literal["superset", "circuit", "alternating_sets"] | None = Field(None, json_schema_extra={"x-response-optional": True})
    id: UUID
    origin: Literal["manual"]
    exercise_type: Literal["strength", "bodyweight", "power", "other"] | None = None
    block_type: ExerciseBlockType | None = Field(None, json_schema_extra={"x-response-optional": True})
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
    programme_week: int | None = Field(None, ge=1, json_schema_extra={"x-response-optional": True})
    id: UUID
    name: Name
    weekday: Weekday | None = None
    scheduled_date: date | None = None
    session_type: Literal["mixed", "strength_power", "conditioning", "skill", "sparring", "recovery", "rest"] = "mixed"
    # The coach's note for the whole session, shown with it on Today. Older
    # saved sessions have none, so responses may leave it out.
    notes: str | None = Field(None, max_length=2000, json_schema_extra={"x-response-optional": True})
    exercises: list[ExerciseInput] = Field(default_factory=list, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def legacy(cls, value):
        return legacy_origins(value)


class CustomPlanInput(DraftModel):
    name: Name
    schedule_mode: Literal["weekly", "dated"] = "weekly"
    start_date: date | None = None
    end_date: date | None = None
    sessions: list[CustomSessionInput] = Field(default_factory=list, max_length=336)

    @model_validator(mode="after")
    def unique_occurrences(self):
        if self.schedule_mode == "weekly":
            if self.start_date or self.end_date or any(s.scheduled_date for s in self.sessions):
                raise ValueError("Weekly plans cannot contain calendar dates.")
            if len(self.sessions) > 28:
                raise ValueError("Weekly plans allow at most 28 sessions.")
        else:
            if not self.start_date or not self.end_date or not 0 <= (self.end_date - self.start_date).days < 84:
                raise ValueError("Choose a date range between 1 and 84 days.")
            counts = {}
            for session in self.sessions:
                if not session.scheduled_date or not self.start_date <= session.scheduled_date <= self.end_date:
                    raise ValueError("Every session needs a date inside the plan range.")
                session.weekday = WEEKDAYS[session.scheduled_date.weekday()]
                monday = session.scheduled_date - timedelta(days=session.scheduled_date.weekday())
                counts[monday] = counts.get(monday, 0) + 1
                if counts[monday] > 28:
                    raise ValueError("Each calendar week allows at most 28 sessions.")
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
    schedule_mode: Literal["weekly", "dated"] = "weekly"
    start_date: date | None = None
    end_date: date | None = None
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


class CoachPlan(CustomPlan):
    fighter_id: UUID
    coach_name: str | None = None
    assigned_at: datetime | None = None
    published_plan_id: UUID | None = None
    assignment_status: Literal['draft', 'pending', 'active', 'declined', 'superseded']
