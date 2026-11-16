"""Read-only coach views of one connected athlete.

Every route first resolves the caller's accepted connection to the athlete.
Without one the athlete is simply not found, so a coach cannot probe for
accounts, and ending the connection ends access on the next request. Nothing
here writes, and the athlete's free-text notes are never returned.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.compliance import evaluate_profile_compliance
from api.contracts.external_training import TrainingHistoryResponse
from api.contracts.session_details import SessionDetails
from api.models import NutritionBodyweightLogEntry, ProfileRecord
from api.nutrition_workspace import build_nutrition_workspace
from api.plan_mappers import _map_profile_row
from api.routes.today import _saved_history_session, history_sessions
from api.services.session_details import MISSING_DETAILS, session_details
from api.store import AppStore

HEALTH_SESSION_TYPES = {"rehab", "recovery"}
PRIVATE_TEXT = ("notes", "takeaway", "modification_reason")
MASKED_TITLE = "Programme session"


@dataclass(frozen=True)
class ConnectedFighter:
    profile: ProfileRecord
    # Each is the connection's choice and the athlete's own health consent.
    share_recovery: bool
    share_injuries: bool


class CoachFighterWeight(BaseModel):
    shared: bool
    entries: list[NutritionBodyweightLogEntry] = Field(default_factory=list)
    # The camp's current weight: the newest weigh-in unless Camp Setup holds a later one.
    current_weight_kg: float | None = None
    target_weight_kg: float | None = None
    rolling_7_day_average_weight: float | None = None
    weekly_pace_kg: float | None = None


def _is_health_session(row: Mapping[str, Any]) -> bool:
    """A plan session that is rehab or recovery, or whose type is unknown."""
    if row.get("source") == "external":
        return False
    session = _saved_history_session(row)
    return session is None or session.get("session_type") in HEALTH_SESSION_TYPES


def _coach_row(row: Mapping[str, Any], fighter: ConnectedFighter) -> dict[str, Any]:
    safe = {key: value for key, value in row.items() if key not in PRIVATE_TEXT}
    safe["history_editable"] = False
    if not fighter.share_injuries:
        safe.pop("pain_after", None)
        if _is_health_session(row):
            safe["title"] = MASKED_TITLE
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
        return ConnectedFighter(athlete, health and connection.get("share_recovery") is True,
                                health and connection.get("share_injuries") is True)

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
        if not fighter.share_injuries:
            # Neither the title nor the notice may reveal unshared rehab work.
            message = None if details.blocks else MISSING_DETAILS
            row = store.get_session_completion_by_id(athlete_id, str(entry_id)) if source == "unlxck" else None
            if row and _is_health_session(row):
                session["title"] = MASKED_TITLE
        return details.model_copy(update={
            "session": details.session.model_copy(update=session),
            "blocks": [block.model_copy(update={"notes": None}) for block in details.blocks],
            "sparring": details.sparring.model_copy(update={"notes": None}) if details.sparring else None,
            "message": message,
        })

    @router.get("/api/coach/fighters/{athlete_id}/weight", response_model=CoachFighterWeight)
    def fighter_weight(fighter: ConnectedFighter = Depends(connected_fighter),
                       store: AppStore = Depends(get_store)):
        if not fighter.share_recovery:
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
