"""A coach's access to one connected athlete.

Every route first resolves the caller's accepted connection to the athlete.
Without one the athlete is simply not found, so a coach cannot probe for
accounts, and ending the connection ends access on the next request.

Reads never return the athlete's free-text notes. The only writes are a session
the coach records for the athlete and the exercises on that session: a coach
can never change a record the athlete, or another coach, entered.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.compliance import evaluate_profile_compliance
from api.contracts.external_training import ExternalTrainingCreate, TrainingHistoryResponse, TrainingHistorySession
from api.contracts.manual_exercises import ManualExerciseEntry, ManualExerciseListResponse, PerformedExerciseInput
from api.contracts.session_details import SessionDetails
from api.models import NutritionBodyweightLogEntry, ProfileRecord
from api.nutrition_workspace import build_nutrition_workspace
from api.plan_mappers import _map_profile_row
from api.routes.today import _saved_history_session, history_sessions
from api.services.manual_exercises import delete_session_exercise, list_session_exercises, save_session_exercise
from api.services.session_details import MISSING_DETAILS, session_details
from api.services.training_history import create_external_training
from api.store import AppStore

HEALTH_SESSION_TYPES = {"rehab", "recovery"}
PRIVATE_TEXT = ("notes", "takeaway", "modification_reason")
MASKED_TITLE = "Programme session"
# Words that name injury or rehab work. Kept in step with
# coach_private.health_session in the coach weight-consent migration.
HEALTH_WORDS = re.compile(r"\b(rehab|prehab|injur|physio|pain|recover|sore|strain|sprain|tendon|tendin|concuss)",
                          re.IGNORECASE)


@dataclass(frozen=True)
class ConnectedFighter:
    profile: ProfileRecord
    coach: ProfileRecord
    # The athlete's own consent to health-data use, which training logs need.
    health_consent: bool
    # Each is the connection's choice and the athlete's own health consent.
    share_recovery: bool
    share_injuries: bool
    # Weight is its own choice; connections accepted before it existed lack it.
    share_weight: bool
    allow_session_logging: bool


class CoachFighterWeight(BaseModel):
    shared: bool
    entries: list[NutritionBodyweightLogEntry] = Field(default_factory=list)
    # The camp's current weight: the newest weigh-in unless Camp Setup holds a later one.
    current_weight_kg: float | None = None
    target_weight_kg: float | None = None
    rolling_7_day_average_weight: float | None = None
    weekly_pace_kg: float | None = None


def _names_health(text: Any) -> bool:
    return isinstance(text, str) and HEALTH_WORDS.search(text) is not None


def _is_health_session(row: Mapping[str, Any]) -> bool:
    """A plan session that could reveal injury work to a coach without injury sharing.

    Rehab and recovery sessions, sessions whose type is unknown, and mixed
    sessions: one carrying a rehab block, or titled for the injury work
    ("Knee Rehab + Strength") while typed as strength.
    """
    if row.get("source") == "external":
        return False
    session = _saved_history_session(row)
    if session is None or session.get("session_type") in HEALTH_SESSION_TYPES:
        return True
    blocks = session.get("blocks") if isinstance(session.get("blocks"), list) else []
    return (_names_health(row.get("title")) or _names_health(session.get("title"))
            or any(isinstance(block, Mapping) and block.get("block_type") == "rehab" for block in blocks))


def _coach_row(row: Mapping[str, Any], fighter: ConnectedFighter) -> dict[str, Any]:
    safe = {key: value for key, value in row.items() if key not in PRIVATE_TEXT}
    safe["history_editable"] = False
    safe["coach_exercises_editable"] = (
        fighter.allow_session_logging and fighter.health_consent and row.get("source") == "external"
        and str(row.get("logged_by_coach_id") or "") == fighter.coach.athlete_id
    )
    if not fighter.share_injuries:
        safe.pop("pain_after", None)
        if _is_health_session(row):
            safe["title"] = MASKED_TITLE
            safe["session_type"] = None
    return safe


def build_coach_router(*, require_profile, get_store) -> APIRouter:
    router = APIRouter(tags=["coach"])

    def connected_fighter(athlete_id: UUID,
                          coach: ProfileRecord = Depends(require_profile),
                          store: AppStore = Depends(get_store)) -> ConnectedFighter:
        fighter_id = str(athlete_id)
        connection = (store.get_accepted_coach_connection(coach.athlete_id, fighter_id)
                      if coach.role in {"coach", "admin"} and coach.access_status == "approved" else None)
        # Defend the boundary as well as the scoped query.
        if (not connection or connection.get("status") != "accepted"
                or str(connection.get("coach_id")) != coach.athlete_id
                or str(connection.get("athlete_id")) != fighter_id):
            raise HTTPException(status_code=404, detail="Fighter not found.")
        row = store.get_profile(fighter_id)
        athlete = _map_profile_row(row) if row else None
        if athlete is None or athlete.role != "athlete" or athlete.access_status != "approved":
            raise HTTPException(status_code=404, detail="Fighter not found.")
        health = evaluate_profile_compliance(athlete).health_consent_granted
        return ConnectedFighter(athlete, coach, health, health and connection.get("share_recovery") is True,
                                health and connection.get("share_injuries") is True,
                                health and connection.get("share_weight") is True,
                                connection.get("allow_session_logging") is True)

    def writable_fighter(fighter: ConnectedFighter = Depends(connected_fighter)) -> ConnectedFighter:
        # The athlete's own logging needs this consent too; a coach cannot
        # collect training data the athlete has not agreed to have held.
        if not fighter.allow_session_logging:
            raise HTTPException(status_code=403, detail="This fighter has not allowed coach session logging.")
        if not fighter.health_consent:
            raise HTTPException(status_code=403, detail="This fighter has not allowed health-data use, so sessions can't be logged for them.")
        return fighter

    def coach_logged_session(entry_id: UUID,
                             fighter: ConnectedFighter = Depends(writable_fighter),
                             store: AppStore = Depends(get_store)) -> str:
        """A live session this coach entered for this fighter; nothing else is theirs to change."""
        row = store.get_external_training_session(fighter.profile.athlete_id, str(entry_id))
        if (not row or row.get("deleted_at") or str(row.get("athlete_id")) != fighter.profile.athlete_id
                or str(row.get("logged_by_coach_id") or "") != fighter.coach.athlete_id):
            raise HTTPException(status_code=404, detail="Training session not found.")
        return str(entry_id)

    @router.post("/api/coach/fighters/{athlete_id}/training/sessions", response_model=TrainingHistorySession, status_code=201)
    def log_fighter_training(body: ExternalTrainingCreate,
                             fighter: ConnectedFighter = Depends(writable_fighter),
                             store: AppStore = Depends(get_store)):
        return create_external_training(
            store, fighter.profile.athlete_id, fighter.profile.athlete_timezone, body,
            logged_by_coach=(fighter.coach.athlete_id, fighter.coach.full_name))

    @router.get("/api/coach/fighters/{athlete_id}/training/sessions/{entry_id}/exercises", response_model=ManualExerciseListResponse)
    def list_fighter_session_exercises(fighter: ConnectedFighter = Depends(writable_fighter),
                                       entry_id: str = Depends(coach_logged_session),
                                       store: AppStore = Depends(get_store)):
        return {"entries": list_session_exercises(store, fighter.profile.athlete_id, entry_id, health_consent_granted=True)}

    @router.put("/api/coach/fighters/{athlete_id}/training/sessions/{entry_id}/exercises/{exercise_entry_id}",
                response_model=ManualExerciseEntry)
    def save_fighter_session_exercise(exercise_entry_id: UUID, body: PerformedExerciseInput,
                                      fighter: ConnectedFighter = Depends(writable_fighter),
                                      entry_id: str = Depends(coach_logged_session),
                                      store: AppStore = Depends(get_store)):
        return save_session_exercise(store, fighter.profile.athlete_id, entry_id, str(exercise_entry_id), body)

    @router.delete("/api/coach/fighters/{athlete_id}/training/sessions/{entry_id}/exercises/{exercise_entry_id}", status_code=204)
    def remove_fighter_session_exercise(exercise_entry_id: UUID,
                                        fighter: ConnectedFighter = Depends(writable_fighter),
                                        entry_id: str = Depends(coach_logged_session),
                                        store: AppStore = Depends(get_store)):
        delete_session_exercise(store, fighter.profile.athlete_id, entry_id, str(exercise_entry_id))
        return Response(status_code=204)

    @router.get("/api/coach/fighters/{athlete_id}/training/history", response_model=TrainingHistoryResponse)
    def fighter_training_history(before_day: date | None = None,
                                 limit: int = Query(default=200, ge=1, le=200),
                                 fighter: ConnectedFighter = Depends(connected_fighter),
                                 store: AppStore = Depends(get_store)):
        page = history_sessions(store, fighter.profile.athlete_id, limit=limit, before_day=before_day,
                                include_skipped=True)
        return {**page, "sessions": [_coach_row(row, fighter) for row in page["sessions"]]}

    @router.get("/api/coach/fighters/{athlete_id}/training/history/{source}/{entry_id}", response_model=SessionDetails)
    def fighter_session_details(source: Literal["unlxck", "external"], entry_id: UUID,
                                fighter: ConnectedFighter = Depends(connected_fighter),
                                store: AppStore = Depends(get_store)):
        athlete_id = fighter.profile.athlete_id
        details = session_details(store, athlete_id, source, str(entry_id),
                                  health_consent_granted=fighter.share_injuries)
        session = {"history_editable": False, "takeaway": "", "notes": None, "modification_reason": None}
        message = details.message
        blocks = details.blocks
        if not fighter.share_injuries:
            # Neither the title, an exercise name nor the notice may reveal
            # unshared rehab work, including inside a mixed strength session.
            blocks = [block for block in blocks if not _names_health(block.name)]
            message = None if blocks else MISSING_DETAILS
            row = store.get_session_completion_by_id(athlete_id, str(entry_id)) if source == "unlxck" else None
            if (row and _is_health_session(row)) or _names_health(details.session.title):
                session.update(title=MASKED_TITLE, session_type=None)
        return details.model_copy(update={
            "session": details.session.model_copy(update=session),
            "blocks": [block.model_copy(update={"notes": None}) for block in blocks],
            "sparring": details.sparring.model_copy(update={"notes": None}) if details.sparring else None,
            "message": message,
        })

    @router.get("/api/coach/fighters/{athlete_id}/weight", response_model=CoachFighterWeight)
    def fighter_weight(fighter: ConnectedFighter = Depends(connected_fighter),
                       store: AppStore = Depends(get_store)):
        if not fighter.share_weight:
            return CoachFighterWeight(shared=False)
        athlete = fighter.profile
        workspace = build_nutrition_workspace(profile=athlete,
                                              latest_intake_row=store.get_latest_intake(athlete.athlete_id))
        return CoachFighterWeight(
            shared=True,
            entries=[entry.model_copy(update={"notes": None})
                     for entry in workspace.nutrition_monitoring.daily_bodyweight_log],
            current_weight_kg=workspace.shared_camp_context.current_weight_kg,
            # Under-18s have no weight-cut feature, so no target is shown for one.
            target_weight_kg=None if athlete.is_minor else workspace.shared_camp_context.target_weight_kg,
            rolling_7_day_average_weight=workspace.derived.rolling_7_day_average_weight,
            weekly_pace_kg=workspace.derived.weekly_pace_kg,
        )

    return router
