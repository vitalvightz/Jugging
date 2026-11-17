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

from api.contracts.custom_plans import CustomPlanInput, CoachPlan
from api.services.coach_plans import coach_plan_fields, assignment_view, assignment_views, saved_input
from api.services.custom_plans import schedule_issues
from api.compliance import evaluate_profile_compliance
from api.contracts.external_training import ExternalTrainingCreate, TrainingHistoryResponse, TrainingHistorySession
from api.contracts.manual_exercises import ManualExerciseEntry, ManualExerciseListResponse, PerformedExerciseInput
from api.contracts.session_details import SessionDetails, SessionDetailBlock
from api.contracts.training_day import current_training_day
from api.routes.nutrition import _active_plan_fight_date
from api.services.active_plan import resolve_active_plan
from api.services.open_plan_timeline import project_open_structured_plan
from api.models import NutritionBodyweightLogEntry, ProfileRecord
from api.nutrition_workspace import build_nutrition_workspace
from api.plan_mappers import _map_profile_row
from api.routes.today import _saved_history_session, history_sessions
from api.services.manual_exercises import delete_session_exercise, list_session_exercises, save_session_exercise
from api.services.session_details import MISSING_DETAILS, session_details, _exercise
from api.services.training_history import create_external_training
from api.store import AppStore

HEALTH_SESSION_TYPES = {"rehab", "recovery"}
PRIVATE_TEXT = ("notes", "takeaway", "modification_reason", "focus_cue")
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


class CoachCutAssessment(BaseModel):
    # Only the athlete's authoritative cut fields, never private readiness flags.
    weight_cut_status: Literal["none", "on_target", "on_track", "behind", "too_aggressive"]
    weight_cut_risk_band: str
    days_until_fight: int | None = None
    kg_to_target: float | None = None
    weight_cut_behind_kg: float | None = None


class CoachPlannedSession(BaseModel):
    id: str
    plan_id: str
    day: str
    title: str
    type: str
    blocks: list[SessionDetailBlock] = Field(default_factory=list)


class CoachFighterWeight(BaseModel):
    shared: bool
    cut_assessment: CoachCutAssessment | None = None
    current_weight_recorded_at: str | None = None
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


class CoachHistorySession(TrainingHistorySession):
    training_category: Literal["strength", "sparring", "rehab", "training"] = "training"


class CoachHistoryResponse(TrainingHistoryResponse):
    sessions: list[CoachHistorySession]


def _coach_row(row: Mapping[str, Any], fighter: ConnectedFighter) -> dict[str, Any]:
    safe = {key: value for key, value in row.items() if key not in PRIVATE_TEXT}
    safe["history_editable"] = False
    saved = _saved_history_session(row) if row.get("source") != "external" else None
    kind = (saved or {}).get("session_type") or row.get("activity")
    safe["training_category"] = ("rehab" if kind in HEALTH_SESSION_TYPES else
                                 "strength" if kind in {"strength", "strength_power", "conditioning"} else
                                 "sparring" if kind == "sparring" else "training")
    safe["coach_exercises_editable"] = (
        fighter.allow_session_logging and fighter.health_consent and row.get("source") == "external"
        and str(row.get("logged_by_coach_id") or "") == fighter.coach.athlete_id
    )
    if not fighter.share_injuries:
        safe.pop("pain_after", None)
        if _is_health_session(row):
            safe["training_category"] = "training"
            safe["title"] = MASKED_TITLE
            safe["session_type"] = None
    return safe


class _RuntimePlanReads:
    """A store whose owner-scoped plan read carries the runtime brief only."""

    def __init__(self, store: AppStore):
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def get_plan_for_athlete(self, plan_id: str, athlete_id: str) -> dict[str, Any] | None:
        return self._store.get_plan_runtime_for_athlete(plan_id, athlete_id)


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

    def authored_plan(plan_id: UUID, fighter: ConnectedFighter = Depends(connected_fighter),
                      store: AppStore = Depends(get_store)):
        row = next((r for r in store.list_coach_plans(fighter.coach.athlete_id, fighter.profile.athlete_id)
                    if str(r['id']) == str(plan_id)), None)
        if not row:
            raise HTTPException(404, 'Plan not found.')
        return row

    @router.get('/api/coach/fighters/{athlete_id}/plans', response_model=list[CoachPlan])
    def coach_plans(fighter: ConnectedFighter = Depends(connected_fighter), store: AppStore = Depends(get_store)):
        return assignment_views(store, store.list_coach_plans(fighter.coach.athlete_id, fighter.profile.athlete_id))

    @router.post('/api/coach/fighters/{athlete_id}/plans', response_model=CoachPlan, status_code=201)
    def create_coach_plan(body: CustomPlanInput, request_id: UUID,
                          fighter: ConnectedFighter = Depends(connected_fighter), store: AppStore = Depends(get_store)):
        fields = coach_plan_fields(store, body, coach_id=fighter.coach.athlete_id)
        row = store.coach_plan_command(fighter.coach.athlete_id, fighter.profile.athlete_id, str(request_id), 'create', fields)
        return assignment_view(store, row)

    @router.get('/api/coach/fighters/{athlete_id}/plans/{plan_id}', response_model=CoachPlan)
    def read_coach_plan(row=Depends(authored_plan), store: AppStore = Depends(get_store)):
        return assignment_view(store, row)

    @router.put('/api/coach/fighters/{athlete_id}/plans/{plan_id}', response_model=CoachPlan)
    def edit_coach_plan(body: CustomPlanInput, row=Depends(authored_plan),
                        fighter: ConnectedFighter = Depends(connected_fighter), store: AppStore = Depends(get_store)):
        fields = coach_plan_fields(store, body, row, coach_id=fighter.coach.athlete_id)
        updated = store.coach_plan_command(fighter.coach.athlete_id, fighter.profile.athlete_id, row['id'], 'update', fields)
        return assignment_view(store, updated)

    @router.delete('/api/coach/fighters/{athlete_id}/plans/{plan_id}', status_code=204)
    def delete_coach_plan(row=Depends(authored_plan), fighter: ConnectedFighter = Depends(connected_fighter),
                          store: AppStore = Depends(get_store)):
        """Take a plan out of the coach's list.

        An unsent draft is deleted. A declined or superseded assignment is only
        hidden from the coach: the fighter's consent record and their own plan
        are theirs and stay. A pending plan is retracted first, and a plan the
        fighter is training on cannot be removed by the coach at all.
        """
        coach_id, fighter_id = fighter.coach.athlete_id, fighter.profile.athlete_id
        status = assignment_view(store, row)['assignment_status']
        if status == 'pending':
            raise HTTPException(409, 'Retract this plan before deleting it.')
        if status == 'active':
            raise HTTPException(409, "This fighter is training on this plan. Send a revised plan instead.")
        if status == 'draft':
            if not store.delete_coach_draft(coach_id, fighter_id, str(row['id'])):
                raise HTTPException(409, 'This plan was sent. Refresh and try again.')
        else:
            store.coach_plan_command(coach_id, fighter_id, row['id'], 'remove')
        return Response(status_code=204)

    @router.post('/api/coach/fighters/{athlete_id}/plans/{plan_id}/retract', response_model=CoachPlan)
    def retract_coach_plan(row=Depends(authored_plan), fighter: ConnectedFighter = Depends(connected_fighter),
                           store: AppStore = Depends(get_store)):
        """Take back a sent plan the fighter has not answered; it becomes a draft again."""
        if row.get('accepted_at') or row.get('declined_at'):
            raise HTTPException(409, 'The fighter has already answered this plan.')
        updated = store.coach_plan_command(fighter.coach.athlete_id, fighter.profile.athlete_id, row['id'], 'retract')
        return assignment_view(store, updated)

    @router.post('/api/coach/fighters/{athlete_id}/plans/{plan_id}/assign', response_model=CoachPlan)
    def assign_coach_plan(row=Depends(authored_plan), fighter: ConnectedFighter = Depends(connected_fighter),
                          store: AppStore = Depends(get_store)):
        # Recheck schedule, exercise availability and private attachments at send.
        fields = coach_plan_fields(store, saved_input(row), row, coach_id=fighter.coach.athlete_id)
        issues = schedule_issues(fields['sessions'])
        if issues:
            raise HTTPException(422, ' '.join(issues))
        updated = store.coach_plan_command(fighter.coach.athlete_id, fighter.profile.athlete_id, row['id'], 'assign')
        return assignment_view(store, updated)

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

    @router.get("/api/coach/fighters/{athlete_id}/training/history", response_model=CoachHistoryResponse)
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
        can_feedback = fighter.health_consent
        message = details.message
        blocks = details.blocks
        if not fighter.share_injuries:
            # Neither the title, an exercise name nor the notice may reveal
            # unshared rehab work, including inside a mixed strength session.
            blocks = [block for block in blocks if not _names_health(block.name)]
            message = None if blocks else MISSING_DETAILS
            row = store.get_session_completion_by_id(athlete_id, str(entry_id)) if source == "unlxck" else None
            if source == "unlxck" and (row is None or _is_health_session(row)):
                can_feedback = False
            if (row and _is_health_session(row)) or _names_health(details.session.title):
                session.update(title=MASKED_TITLE, session_type=None)
        return details.model_copy(update={
            "coach_feedback_allowed": can_feedback,
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
                                              latest_intake_row=store.get_latest_intake(athlete.athlete_id),
                                              active_plan_fight_date=_active_plan_fight_date(store, athlete))
        return CoachFighterWeight(
            shared=True,
            cut_assessment=None if athlete.is_minor else CoachCutAssessment.model_validate(
                workspace.derived.model_dump()),
            current_weight_recorded_at=workspace.shared_camp_context.current_weight_recorded_at,
            entries=[entry.model_copy(update={"notes": None})
                     for entry in workspace.nutrition_monitoring.daily_bodyweight_log],
            current_weight_kg=workspace.shared_camp_context.current_weight_kg,
            # Under-18s have no weight-cut feature, so no target is shown for one.
            target_weight_kg=None if athlete.is_minor else workspace.shared_camp_context.target_weight_kg,
            rolling_7_day_average_weight=workspace.derived.rolling_7_day_average_weight,
            weekly_pace_kg=workspace.derived.weekly_pace_kg,
        )

    @router.get("/api/coach/fighters/{athlete_id}/training/plan", response_model=list[CoachPlannedSession])
    def fighter_plan(include_past: bool = False, fighter: ConnectedFighter = Depends(connected_fighter),
                     store: AppStore = Depends(get_store)):
        athlete = fighter.profile
        today = current_training_day(athlete_timezone=athlete.athlete_timezone)
        # Only the calendar is read here, so an eligible pointer is read with
        # the runtime brief (no candidate_pools), as the plan calendar does.
        # Every other outcome keeps the full resolution, auto-activation included.
        runtime = resolve_active_plan(_RuntimePlanReads(store), athlete.athlete_id,
                                      current_training_day=today, auto_activate=False)
        if runtime.source == "explicit":
            plan = runtime.plan
        elif runtime.source == "read_failure":
            plan = None  # authority reads fail closed, as before
        else:
            plan = resolve_active_plan(store, athlete.athlete_id, current_training_day=today).plan
        if not plan:
            return []
        structured = plan.get("structured_plan")
        if not isinstance(structured, Mapping):
            return []
        projected, _ = project_open_structured_plan(plan, structured, current_training_day=today.isoformat())
        result = {}
        for week in projected.get("weeks") or []:
            for day in week.get("days") or []:
                iso = str(day.get("date") or "")[:10]
                if not include_past and iso < today.isoformat():
                    continue
                try:
                    date.fromisoformat(iso)
                except ValueError:
                    continue
                for session in day.get("sessions") or []:
                    session_id = str(session.get("session_id") or "")
                    if not session_id:
                        continue
                    blocks = [b for b in session.get("blocks") or [] if isinstance(b, Mapping)]
                    health = (session.get("session_type") in HEALTH_SESSION_TYPES
                              or _names_health(session.get("title"))
                              or any(b.get("block_type") == "rehab" for b in blocks))
                    if not fighter.share_injuries and health:
                        continue
                    safe_blocks = [_exercise(b, None, False) for b in blocks
                                   if fighter.share_injuries or not _names_health(b.get("display_name"))]
                    result[(iso, session_id)] = CoachPlannedSession(
                        id=session_id, plan_id=str(plan["id"]), day=iso,
                        title=str(session.get("title") or "Programme session"),
                        type=str(session.get("session_type") or "training"), blocks=safe_blocks)
        return sorted(result.values(), key=lambda row: (row.day, row.id))

    return router
