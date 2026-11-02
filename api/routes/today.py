"""Block 4 Today/Overview API surface.

Thin HTTP wrappers over the fail-safe Today service boundary. The server owns the
training-day calculation and the recommendation; endpoints never trust a
client-supplied recommendation and never mutate the saved plan. This is the
non-UI backend integration — no Today UI is built here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any, Literal, Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response

from api.contracts.external_training import ExternalTrainingCreate, ExternalTrainingFields, ExternalTrainingTakeaway, TrainingHistorySession, TrainingHistoryResponse
from api.services.training_history import (
    training_history_page, create_external_training, update_external_training,
    delete_external_training, set_external_training_takeaway,
    delete_plan_completion, edit_plan_completion, plan_completion_history_editable,
)
from api.contracts.session_details import SessionDetails
from api.services.session_details import session_details
from api.contracts.exercise_library import ExerciseLibraryResponse, ExercisePerformancesResponse
from api.contracts.manual_exercises import ManualExerciseEntry, PerformedExerciseInput, ManualExerciseListResponse
from api.services.manual_exercises import delete_session_exercise, list_session_exercises, save_session_exercise
from api.services.tracking_xp import reconcile_training_xp
from api.services.training_focus import latest_training_focus
from api.services.exercise_library import exercise_library, exercise_performances

from api.compliance_guards import require_health_feature_access
from api.compliance import evaluate_profile_compliance
from api.models import (
    ExerciseHistoryResponse,
    ExerciseLogBatchRequest,
    ExerciseLogBatchResponse,
    ExerciseLogListResponse,
    ExerciseLogRecord,
    ExerciseLogRequest,
    ExerciseLogResponse,
    InjuryFlagRecord,
    LandingResponse,
    PendingRehabResponseSetResponse,
    PendingRehabResponsesResponse,
    ProfileRecord,
    RehabResponseRequest,
    RehabResponseResult,
    SessionCompletionHistoryResponse,
    SessionCompletionRequest,
    SessionCompletionResponse,
    SparringLogHistoryResponse,
    SparringLogRecord,
    SparringLogRequest,
    SparringLogResponse,
    TrainingFocusResponse,
    SparringWindowSummary,
    TodayCheckinRecord,
    TodayCheckinRequest,
    TodayCheckinResponse,
    TodayInjuryCheckinRequest,
    TodayInjuryCheckinResponse,
)
from api.contracts.command_view import CommandView
from api.contracts.completion import HistoryCompletionEdit, completion_landing_state, completion_status_of
from api.contracts.rehab_completion import COMPLETED_STATUSES
from api.services.exercise_log_service import (
    list_exercise_logs_for_today,
    exercise_history_context,
    record_exercise_log,
    record_exercise_logs,
)
from api.services.progress_notifications import award_session_progress
from api.services.completion_reads import CompletionReads
from api.services.rehab_completion_service import (
    build_rehab_response_contexts,
    list_pending_rehab_response_sets,
    rehab_response_contexts_by_injury,
    record_rehab_exposures,
    record_reported_permission_rehab,
)
from api.services.notification_foundation import invalidate_notification_action
from api.services.today_command_cache import remember_today_command
from api.services.today_service import resolve_training_day
from api.services.effective_structured_plan import resolve_effective_structured_plan
from api.services.open_plan_timeline import project_open_structured_plan
from api.services.injury_episode_service import InjuryEpisodeObservation, record_episode_observation
from api.services.week_progress import try_award_completed_week_for_completion
from api.services.streaks import reconcile_adherence_streak, reconcile_training_streak
from api.services.xp_awards import (
    award_checkin_xp,
    award_injury_update_xp,
    plan_completion_xp_eligible,
)
from api.services.today_readiness_boundary import (
    build_today_command_view,
    resolve_today_landing,
    submit_today_checkin,
    submit_today_injury_checkin,
    upsert_session_completion,
)
from api.store import AppStore

logger = logging.getLogger(__name__)
PENDING_REHAB_COMPLETION_LIMIT = 500

# Shown to the athlete whenever they log being rocked or dropped. It is a
# safety message, not a diagnosis: stop contact, watch for red flags, tell the
# coach, and get checked if any appear.
ROCKED_SAFETY_NOTICE = (
    "You logged being rocked or dropped. No more contact today. If you get a "
    "headache, dizziness, nausea, confusion, memory problems or blurred vision, "
    "stop training and get checked by a medical professional. Tell your coach."
)
_SPARRING_INTENSITY_LABEL = {"light": "light", "medium": "medium", "hard": "hard"}
_HEAD_CONTACT_LABEL = {"none": "no head contact", "light": "light head contact", "heavy": "heavy head contact"}


def _rocked_review_reason(body: SparringLogRequest) -> str:
    rounds = f"{body.rounds_completed} round{'s' if body.rounds_completed != 1 else ''}"
    return (
        "Sparring log: athlete reported being rocked or dropped "
        f"({_SPARRING_INTENSITY_LABEL[body.intensity]} sparring, {rounds}, "
        f"{_HEAD_CONTACT_LABEL[body.head_contact]}). Check in before their next contact session."
    )


def _sparring_log_record(row: Mapping[str, Any]) -> SparringLogRecord:
    return SparringLogRecord.model_validate(
        {
            **row,
            "external_session_id": str(row["external_session_id"]) if row.get("external_session_id") else None,
            "training_day": str(row.get("training_day") or "")[:10],
            "created_at": str(row.get("created_at") or ""),
        }
    )


def _apply_sparring_focus(store: AppStore, athlete_id: str, row: dict[str, Any], body: Any) -> dict[str, Any]:
    """Write the optional focus once this request's answers are the current ones.

    A late, older correction never overwrites a newer focus. A failed write keeps
    the saved log; the returned focus then differs and the client says so.
    """
    if str(row.get("focus") or "") == body.focus or not row.get("id"):
        return row
    if int(row.get("detail_revision") or 0) != body.detail_revision:
        return row
    try:
        updated = store.set_sparring_log_focus(athlete_id, str(row["id"]), body.focus, revision=body.detail_revision)
    except Exception:  # noqa: BLE001 - the sparring log itself is saved
        logger.exception("[sparring] focus write failed athlete_id=%s", athlete_id)
        return row
    return {**row, "focus": updated.get("focus", body.focus)} if updated else row


def _sparring_window(rows: list[SparringLogRecord], *, today: date, days: int) -> SparringWindowSummary:
    """Totals for the ``days`` athlete-local days ending ``today`` (inclusive)."""
    first_day = (today - timedelta(days=days - 1)).isoformat()
    last_day = today.isoformat()
    window = [row for row in rows if first_day <= row.training_day <= last_day]
    return SparringWindowSummary(
        days=days,
        sessions=len(window),
        rounds=sum(row.rounds_completed for row in window),
        hard_rounds=sum(row.rounds_completed for row in window if row.intensity == "hard"),
        hard_days=len({row.training_day for row in window if row.intensity == "hard"}),
        heavy_head_contact_sessions=sum(1 for row in window if row.head_contact == "heavy"),
        rocked_count=sum(1 for row in window if row.rocked),
    )


def _exercise_log_record(row: Mapping[str, Any]) -> ExerciseLogRecord:
    return ExerciseLogRecord.model_validate(
        {
            **row,
            "training_day": str(row.get("training_day") or "")[:10],
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }
    )


def _checkin_record(row: dict[str, Any]) -> TodayCheckinRecord:
    triggers = row.get("recommendation_triggers") or []
    if not isinstance(triggers, list):
        triggers = []
    return TodayCheckinRecord(**{**row, "recommendation_triggers": list(triggers)})


def _saved_session_title(completion: Mapping[str, Any]) -> str | None:
    """Read owned historical evidence, never current-plan content.

    Older snapshots omit identity metadata; when supplied, it must match the
    completion. Malformed or incomplete snapshots use the existing fallback.
    """
    snapshot = completion.get("prescription_snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    session = snapshot.get("session")
    if not isinstance(session, Mapping):
        return None
    for source, key in ((snapshot, "plan_id"), (snapshot, "training_day"), (session, "session_id")):
        if source.get(key) is not None and str(source[key]) != str(completion.get(key)):
            return None
    title = session.get("title")
    return title if isinstance(title, str) and title.strip() else None


def _history_title(store: AppStore, athlete_id: str, completion: Mapping[str, Any], plans: dict) -> str | None:
    title = _saved_session_title(completion)
    if title is not None:
        return title
    plan_id = str(completion.get("plan_id") or "")
    if not plan_id:
        return None
    if plan_id not in plans:
        plan = store.get_training_plan_for_athlete(plan_id, athlete_id)
        structured = resolve_effective_structured_plan(plan) if plan else None
        plans[plan_id] = (plan, structured) if plan and structured else None
    resolved = plans[plan_id]
    return _session_history_title(*resolved, completion) if resolved else None


def _session_history_title(
    plan_row: Mapping[str, Any],
    structured_plan: Mapping[str, Any],
    completion: Mapping[str, Any],
) -> str | None:
    training_day = str(completion.get("training_day") or "")
    session_id = str(completion.get("session_id") or "")
    projected, _ = project_open_structured_plan(
        plan_row, structured_plan, current_training_day=training_day
    )
    for week in projected.get("weeks") or []:
        if not isinstance(week, Mapping):
            continue
        for day in week.get("days") or []:
            if not isinstance(day, Mapping) or str(day.get("date") or "")[:10] != training_day:
                continue
            sessions = [session for session in day.get("sessions") or [] if isinstance(session, Mapping)]
            matched = next(
                (session for session in sessions if str(session.get("session_id") or "") == session_id),
                None,
            )
            if matched is None and len(sessions) == 1 and session_id == training_day:
                matched = sessions[0]
            today_card = day.get("today_card") if isinstance(day.get("today_card"), Mapping) else {}
            if matched is not None:
                title = matched.get("title") or today_card.get("headline") or matched.get("session_type")
            elif not sessions and session_id == training_day:
                title = today_card.get("headline")
            else:
                continue
            return str(title).strip() or None
    return None


def build_today_router(*, require_profile, get_store) -> APIRouter:
    router = APIRouter(tags=["today"])

    @router.post("/api/training/sessions", response_model=TrainingHistorySession, status_code=201)
    def log_external_training(body: ExternalTrainingCreate,
                              profile: ProfileRecord = Depends(require_profile),
                              store: AppStore = Depends(get_store)):
        require_health_feature_access(profile)
        return create_external_training(store, profile.athlete_id, profile.athlete_timezone, body)

    @router.patch("/api/training/sessions/{entry_id}", response_model=TrainingHistorySession)
    def edit_external_training(entry_id: UUID, body: ExternalTrainingFields,
                               profile: ProfileRecord = Depends(require_profile),
                               store: AppStore = Depends(get_store)):
        require_health_feature_access(profile)
        return update_external_training(store, profile.athlete_id, str(entry_id), body)

    @router.put("/api/training/sessions/{entry_id}/takeaway", response_model=TrainingHistorySession)
    def save_external_training_takeaway(entry_id: UUID, body: ExternalTrainingTakeaway,
                                        profile: ProfileRecord = Depends(require_profile),
                                        store: AppStore = Depends(get_store)):
        require_health_feature_access(profile)
        return set_external_training_takeaway(store, profile.athlete_id, str(entry_id), body)

    @router.delete("/api/training/sessions/{entry_id}", status_code=204)
    def remove_external_training(entry_id: UUID,
                                 profile: ProfileRecord = Depends(require_profile),
                                 store: AppStore = Depends(get_store)):
        delete_external_training(store, profile.athlete_id, str(entry_id))
        return Response(status_code=204)

    # Optional exercise results on a manual session. Writes record health data
    # and need consent; none of them touches workload, XP, rehab or sparring.
    @router.get("/api/training/sessions/{entry_id}/exercises", response_model=ManualExerciseListResponse)
    def list_manual_exercises(entry_id: UUID, profile: ProfileRecord = Depends(require_profile),
                              store: AppStore = Depends(get_store)):
        return {"entries": list_session_exercises(store, profile.athlete_id, str(entry_id),
            health_consent_granted=evaluate_profile_compliance(profile).health_consent_granted)}

    @router.put("/api/training/sessions/{entry_id}/exercises/{exercise_entry_id}", response_model=ManualExerciseEntry)
    def save_manual_exercise(entry_id: UUID, exercise_entry_id: UUID, body: PerformedExerciseInput,
                             profile: ProfileRecord = Depends(require_profile),
                             store: AppStore = Depends(get_store)):
        require_health_feature_access(profile)
        saved = save_session_exercise(store, profile.athlete_id, str(entry_id), str(exercise_entry_id), body)
        reconcile_training_xp(store, profile.athlete_id, athlete_timezone=profile.athlete_timezone)
        return saved

    @router.delete("/api/training/sessions/{entry_id}/exercises/{exercise_entry_id}", status_code=204)
    def remove_manual_exercise(entry_id: UUID, exercise_entry_id: UUID,
                               profile: ProfileRecord = Depends(require_profile),
                               store: AppStore = Depends(get_store)):
        delete_session_exercise(store, profile.athlete_id, str(entry_id), str(exercise_entry_id))
        return Response(status_code=204)

    @router.get("/api/training/history", response_model=TrainingHistoryResponse)
    def list_training_history(limit: int = Query(default=30, ge=1, le=200), include_skipped: bool = False, manual_only: bool = False,
                              before_day: date | None = None,
                              profile: ProfileRecord = Depends(require_profile),
                              store: AppStore = Depends(get_store)):
        # Pages hold whole training days, so a page can run past ``limit`` rows.
        rows, next_before_day = training_history_page(
            store, profile.athlete_id, limit=limit, before_day=before_day.isoformat() if before_day else None,
            include_skipped=include_skipped, manual_only=manual_only)
        plans = {}
        for row in rows:
            if row["source"] == "external":
                continue
            row["title"] = _history_title(store, profile.athlete_id, row, plans) or "Unlxck session"
            row["history_editable"] = bool(row.get("id")) and plan_completion_history_editable(row)
            row["id"] = str(row.get("id") or row.get("session_id"))
        counts = store.count_manual_exercises(profile.athlete_id, [str(row["id"]) for row in rows if row["source"] == "external"])
        for row in rows:
            if row["source"] == "external":
                row["exercise_count"] = counts.get(str(row["id"]), 0)
        return {"sessions": rows, "next_before_day": next_before_day}

    # Limited History changes to a finished plan session. Removing it marks the
    # session not done again, so streaks are recounted; earned XP is kept, as
    # for removed manual sessions.
    @router.patch("/api/training/history/unlxck/{completion_id}", response_model=TrainingHistorySession)
    def edit_history_plan_session(completion_id: UUID, body: HistoryCompletionEdit,
                                  profile: ProfileRecord = Depends(require_profile),
                                  store: AppStore = Depends(get_store)):
        require_health_feature_access(profile)
        row = edit_plan_completion(store, profile.athlete_id, str(completion_id), body,
            health_consent_granted=evaluate_profile_compliance(profile).health_consent_granted)
        return {**row, "source": "unlxck", "id": str(row["id"]), "history_editable": True,
                "title": _history_title(store, profile.athlete_id, row, {}) or "Unlxck session"}

    @router.delete("/api/training/history/unlxck/{completion_id}", status_code=204)
    def remove_history_plan_session(completion_id: UUID,
                                    profile: ProfileRecord = Depends(require_profile),
                                    store: AppStore = Depends(get_store)):
        row = delete_plan_completion(store, profile.athlete_id, str(completion_id))
        if row is None:
            return Response(status_code=204)
        for reconcile in (reconcile_training_streak, reconcile_adherence_streak):
            try:
                reconcile(store, athlete_id=profile.athlete_id, athlete_timezone=profile.athlete_timezone)
            except Exception:  # noqa: BLE001 - the removal is authoritative
                logger.exception("[streak] reconciliation after history removal failed athlete_id=%s", profile.athlete_id)
        try:
            invalidate_notification_action(
                store, profile_id=profile.athlete_id,
                action_key=f"complete-session:{row.get('session_id')}",
                training_day=str(row.get("training_day") or ""),
                source_metadata={"completion_id": str(row.get("id") or "")},
            )
        except Exception:  # noqa: BLE001
            logger.exception("[notification] history removal invalidation failed profile_id=%s", profile.athlete_id)
        return Response(status_code=204)

    @router.get("/api/training/history/{source}/{entry_id}", response_model=SessionDetails)
    def get_training_session_details(source: Literal["unlxck", "external"], entry_id: UUID,
                                     profile: ProfileRecord = Depends(require_profile),
                                     store: AppStore = Depends(get_store)):
        return session_details(store, profile.athlete_id, source, str(entry_id),
            health_consent_granted=evaluate_profile_compliance(profile).health_consent_granted)

    @router.post("/api/today/injury-episode-observation")
    def injury_episode_observation(
        request_body: InjuryEpisodeObservation,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> dict[str, Any]:
        require_health_feature_access(profile)
        return record_episode_observation(store, athlete_id=profile.athlete_id, observation=request_body,
                                         training_day=resolve_training_day(profile.athlete_timezone), athlete_timezone=profile.athlete_timezone)

    @router.post(
        "/api/today/checkin",
        response_model=TodayCheckinResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_checkin(
        request_body: TodayCheckinRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> TodayCheckinResponse:
        # A readiness check-in collects soreness, fatigue, sleep and pain: health
        # data under Art. 9. No consent, no new collection.
        require_health_feature_access(profile)
        row = submit_today_checkin(
            store,
            athlete_id=profile.athlete_id,
            athlete_timezone=profile.athlete_timezone,
            payload=request_body.model_dump(),
        )
        award_checkin_xp(store, athlete_id=profile.athlete_id, checkin=row)
        record = _checkin_record(row)
        try:
            invalidate_notification_action(
                store,
                profile_id=profile.athlete_id,
                action_key=f"checkin:{record.training_day}",
                training_day=record.training_day,
                source_metadata={"checkin_id": str(row.get("id") or "")},
            )
        except Exception:  # noqa: BLE001 - notification state must not roll back check-in
            logger.exception(
                "[notification] check-in action invalidation failed profile_id=%s",
                profile.athlete_id,
            )
        signal = row.get("readiness_signal") or {}
        return TodayCheckinResponse(
            checkin=record,
            training_day=record.training_day,
            recommendation_state=record.recommendation_state,
            recommendation_reason=record.recommendation_reason,
            triggers=record.recommendation_triggers,
            warnings=[str(warning) for warning in row.get("warnings", [])],
            decision=str(signal.get("decision") or record.recommendation_state),
            decision_tier=str(signal.get("decision_tier") or ""),
            display_state=str(signal.get("display_state") or ""),
            reason_codes=[str(code) for code in signal.get("reason_codes", [])],
            title=str(signal.get("title") or ""),
            detail=str(signal.get("detail") or ""),
            action=str(signal.get("action") or ""),
            safety=str(signal.get("safety") or ""),
            blocks_training=bool(signal.get("blocks_training", False)),
        )

    @router.post(
        "/api/today/injury-checkin",
        response_model=TodayInjuryCheckinResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_injury_checkin(
        request_body: TodayInjuryCheckinRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> TodayInjuryCheckinResponse:
        # Pin the injury write and its calendar-scoped XP award to one instant so
        # a request crossing the 03:00 training-day rollover cannot split them
        # across two different athlete-local days.
        require_health_feature_access(profile)
        request_now = datetime.now(timezone.utc)
        result = submit_today_injury_checkin(
            store,
            athlete_id=profile.athlete_id,
            athlete_timezone=profile.athlete_timezone,
            payload=request_body.model_dump(),
            now=request_now,
        )
        # The injury service owns the write but returns only the refreshed injury
        # state. Resolve the same server-authoritative athlete-local day here
        # rather than expecting an undocumented result field that is never set.
        training_day = resolve_training_day(profile.athlete_timezone, now=request_now)
        # One successful declaration batch earns one daily reward. Returning all
        # open injury rows must never multiply XP, and an empty/no-op request
        # cannot farm the reward merely by re-reading existing injuries.
        award_injury_update_xp(
            store,
            athlete_id=profile.athlete_id,
            training_day=training_day,
            updated_injuries=request_body.injuries,
        )
        # The refreshed response contains every open injury, including untouched
        # flags. Only invalidate follow-ups for flags this request changed.
        for updated_injury_id in result.get("updated_injury_ids", []):
            injury_id = str(updated_injury_id or "").strip()
            if not injury_id:
                continue
            try:
                invalidate_notification_action(
                    store,
                    profile_id=profile.athlete_id,
                    action_key=f"update-injury:{injury_id}",
                    training_day=training_day,
                    completed_at=request_now,
                    source_metadata={"injury_id": injury_id},
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[notification] injury action invalidation failed profile_id=%s injury_id=%s",
                    profile.athlete_id,
                    injury_id,
                )
        return TodayInjuryCheckinResponse(
            open_injuries=[InjuryFlagRecord(**row) for row in result.get("open_injuries", [])],
        )

    @router.get(
        "/api/today/session-completions",
        response_model=list[SessionCompletionHistoryResponse],
    )
    def list_session_completion_history(
        limit: int = Query(default=30, ge=1, le=200),
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> list[SessionCompletionHistoryResponse]:
        rows = store.list_session_completions(profile.athlete_id, limit=limit)
        plans: dict[str, tuple[dict[str, Any], dict[str, Any]] | None] = {}
        history = []
        for row in rows:
            title = _history_title(store, profile.athlete_id, row, plans)
            history.append(SessionCompletionHistoryResponse(**row, session_title=title))
        return history

    @router.get(
        "/api/today/checkins",
        response_model=list[TodayCheckinRecord],
    )
    def list_checkin_history(
        limit: int = Query(default=30, ge=1, le=200),
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> list[TodayCheckinRecord]:
        rows = store.list_today_checkins(profile.athlete_id, limit=limit)
        return [_checkin_record(row) for row in rows]

    @router.get("/api/today", response_model=CommandView)
    def get_today_state(
        response: Response,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> CommandView:
        response.headers["Cache-Control"] = "private, no-store"
        view = build_today_command_view(
            store,
            athlete_id=profile.athlete_id,
            athlete_timezone=profile.athlete_timezone,
        )
        # XP progress refreshes right after every Today load; let it reuse
        # this build instead of assembling the whole command view again.
        remember_today_command(
            store,
            athlete_id=profile.athlete_id,
            athlete_timezone=profile.athlete_timezone,
            view=view,
        )
        return view

    @router.get("/api/today/landing", response_model=LandingResponse)
    def get_landing(
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> LandingResponse:
        has_interacted = store.get_latest_plan_status(profile.athlete_id) is not None
        decision = resolve_today_landing(
            store,
            athlete_id=profile.athlete_id,
            athlete_timezone=profile.athlete_timezone,
            has_interacted=has_interacted,
        )
        return LandingResponse(
            target=decision.target,
            cta=decision.cta,
            row=decision.row,
            reason=decision.reason,
        )

    @router.post(
        "/api/today/session-completion",
        response_model=SessionCompletionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def update_session_completion(
        request_body: SessionCompletionRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> SessionCompletionResponse:
        payload = request_body.model_dump()
        if not evaluate_profile_compliance(profile).health_consent_granted:
            # Completion is mixed-purpose: preserve the training log while
            # dropping the sole health field after consent withdrawal.
            payload["pain_after"] = None
        row = upsert_session_completion(
            store,
            athlete_id=profile.athlete_id,
            athlete_timezone=profile.athlete_timezone,
            payload=payload,
        )
        completion_status = completion_status_of(row)
        # All consumers see the persisted completion. Share only post-save
        # plan/history reads across streaks, XP, week progress and rehab prompts.
        store = CompletionReads(store)
        if completion_status in {"done", "modified", "skipped"}:
            try:
                invalidate_notification_action(
                    store,
                    profile_id=profile.athlete_id,
                    action_key=f"complete-session:{row.get('session_id') or request_body.session_id}",
                    training_day=str(row.get("training_day") or request_body.training_day),
                    source_metadata={"completion_id": str(row.get("id") or "")},
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[notification] session action invalidation failed profile_id=%s",
                    profile.athlete_id,
                )
            # Training consistency and plan adherence are both derived from
            # completion history, never from the narrower reward rules.
            try:
                reconcile_training_streak(
                    store,
                    athlete_id=profile.athlete_id,
                    athlete_timezone=profile.athlete_timezone,
                )
            except Exception:  # noqa: BLE001 - completion remains authoritative
                logger.exception(
                    "[streak] training reconciliation failed athlete_id=%s",
                    profile.athlete_id,
                )
            try:
                reconcile_adherence_streak(
                    store,
                    athlete_id=profile.athlete_id,
                    athlete_timezone=profile.athlete_timezone,
                )
            except Exception:  # noqa: BLE001 - completion remains authoritative
                logger.exception(
                    "[streak] adherence reconciliation failed athlete_id=%s",
                    profile.athlete_id,
                )
        # Preserve the completion record, but only the single server-resolved
        # active plan may drive XP. This closes the overlapping/inactive-plan
        # path without deleting legitimate history or retro logs.
        if plan_completion_xp_eligible(
            store,
            athlete_id=profile.athlete_id,
            completion=row,
        ):
            award_session_progress(
                store,
                athlete_id=profile.athlete_id,
                athlete_timezone=profile.athlete_timezone,
                completion=row,
            )
            try_award_completed_week_for_completion(
                store,
                athlete_id=profile.athlete_id,
                athlete_timezone=profile.athlete_timezone,
                completion=row,
            )
        return SessionCompletionResponse(
            completion=row,
            completion_status=completion_status,
            landing_session_state=completion_landing_state(completion_status),
            rehab_response_prompts=_rehab_prompts_for_completion(
                store, profile=profile, completion=row, simple_rehab=request_body.rehab_tracking == "injury_checkin"
            ),
        )

    def _plan_row_for_completion(
        store: AppStore, *, profile: ProfileRecord, plan_id: str | None
    ) -> dict[str, Any] | None:
        if not plan_id:
            # Standalone work is resolved only from its saved prescription.
            return {"id": None}
        return store.get_plan_for_athlete(plan_id, profile.athlete_id)

    def _rehab_prompts_for_completion(
        store: AppStore, *, profile: ProfileRecord, completion: dict[str, Any], simple_rehab: bool = False
    ) -> list[dict[str, Any]]:
        """The injury-specific prompts this completion raises, or none.

        Health-gated: an athlete who has not granted health consent is not asked
        about an injury, and a resolution failure must never take the completion
        down with it — the session is logged either way.
        """
        if completion_status_of(completion) not in COMPLETED_STATUSES:
            # Checked before the plan read: starting and skipping a session are
            # the common taps, and neither can produce an exposure.
            return []
        if not evaluate_profile_compliance(profile).health_consent_granted:
            return []
        try:
            # Existing context is immutable. Re-saving an old completion must
            # never resolve it against whatever injury episode happens to be
            # open now. Only the transition that first completes the row may
            # initialize a NULL context field.
            if completion.get("rehab_response_contexts") is None:
                if not completion.get("_entered_completed_status"):
                    return []
                plan_row = _plan_row_for_completion(
                    store, profile=profile, plan_id=str(completion.get("plan_id") or "")
                )
                if not plan_row:
                    return []
                _prompts, contexts = build_rehab_response_contexts(
                    store,
                    athlete_id=profile.athlete_id,
                    plan_row=plan_row,
                    training_day=str(completion.get("training_day") or ""),
                    session_id=str(completion.get("session_id") or ""),
                    completion=completion,
                )
                persisted = store.initialize_session_completion_rehab_contexts(
                    profile.athlete_id,
                    completion_id=str(completion.get("id") or ""),
                    plan_id=completion.get("plan_id"),
                    session_id=str(completion.get("session_id") or ""),
                    training_day=str(completion.get("training_day") or ""),
                    contexts=contexts,
                )
                if not persisted or persisted.get("rehab_response_contexts") is None:
                    raise RuntimeError("rehab response context was not persisted")
                completion.update(persisted)
            if simple_rehab and completion.get("rehab_response_contexts"):
                record_reported_permission_rehab(store, athlete_id=profile.athlete_id,
                    plan_row=_plan_row_for_completion(store, profile=profile, plan_id=completion["plan_id"]),
                    completion=completion)
            pending = list_pending_rehab_response_sets(
                store,
                athlete_id=profile.athlete_id,
                completions=[completion],
            )
        except Exception:  # noqa: BLE001 - the completion record is authoritative
            logger.exception(
                "[rehab] response context persistence failed athlete_id=%s completion_id=%s",
                profile.athlete_id,
                completion.get("id"),
            )
            return []
        return pending[0]["rehab_response_prompts"] if pending else []

    @router.get(
        "/api/today/rehab-responses/pending",
        response_model=PendingRehabResponsesResponse,
    )
    def get_pending_rehab_responses(
        plan_id: UUID = Query(),
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> PendingRehabResponsesResponse:
        """Rehydrate valid unanswered prompts across one exact active plan."""
        require_health_feature_access(profile)
        plan_id_value = str(plan_id)
        if not store.get_plan_identity_for_athlete(plan_id_value, profile.athlete_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        # Read one sentinel row beyond the bounded window. Omitted history is
        # never treated as answered or mutated; the explicit flag lets clients
        # and monitoring distinguish a complete read from a bounded one.
        completions = store.list_plan_session_completions(
            profile.athlete_id,
            plan_id_value,
            limit=PENDING_REHAB_COMPLETION_LIMIT + 1,
        )
        history_truncated = len(completions) > PENDING_REHAB_COMPLETION_LIMIT
        # Offered on the session's day and the day after, then dropped.
        earliest_day = (
            date.fromisoformat(resolve_training_day(profile.athlete_timezone)) - timedelta(days=1)
        ).isoformat()
        response_sets = [
            PendingRehabResponseSetResponse(**item)
            for item in list_pending_rehab_response_sets(
                store,
                athlete_id=profile.athlete_id,
                completions=completions[:PENDING_REHAB_COMPLETION_LIMIT],
                earliest_training_day=earliest_day,
            )
        ]
        return PendingRehabResponsesResponse(
            response_sets=response_sets,
            history_truncated=history_truncated,
        )

    @router.post(
        "/api/today/sparring-log",
        response_model=SparringLogResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_sparring_log(
        request_body: SparringLogRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> SparringLogResponse:
        """Record one block of sparring rounds from the round timer, or the
        detail of a manually logged sparring session.

        Head contact and a rocked/dropped report are health data, so the
        health-feature gate applies. The day is the server's athlete-local
        training day, or the linked manual session's own day. A rocked/dropped
        report opens an admin review (not deduplicated against other pending
        reviews: each one is a separate head-impact event a human should see).
        A retried request (same ``client_request_id``) returns the original row
        and review. A manual session has one detail row: a later report corrects
        it in place and can never withdraw a head-impact report. Today shows a
        no-contact notice for the rest of a day with a rocked report.
        """
        require_health_feature_access(profile)
        plan_id = request_body.plan_id
        if plan_id is not None:
            try:
                UUID(plan_id)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
            plan_row = store.get_plan(plan_id)
            if not plan_row or str(plan_row.get("athlete_id")) != str(profile.athlete_id):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        if request_body.external_session_id is not None:
            # The RPC re-checks this inside its transaction; this read gives a
            # clean 404 instead of a retryable failure for a missing session.
            manual = store.get_external_training_session(profile.athlete_id, str(request_body.external_session_id))
            if not manual or manual.get("activity") != "sparring":
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sparring session not found")

        # The focus is not safety data: it is written after the RPC, which stays unchanged.
        fields = request_body.model_dump(mode="json", exclude={"plan_id", "focus"})
        # The log and (for a rocked report) its admin review are written in one
        # transaction. If either fails, nothing is kept and the athlete is told
        # to retry: a rocked report is never acknowledged without its review.
        try:
            result = store.record_sparring_log(
                profile.athlete_id,
                {
                    **fields,
                    "plan_id": plan_id,
                    "training_day": resolve_training_day(profile.athlete_timezone),
                },
                review_reason=_rocked_review_reason(request_body) if request_body.rocked else None,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a retryable failure
            logger.exception("[sparring] log write failed athlete_id=%s", profile.athlete_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Your sparring log was not saved. Please try again.",
            ) from exc
        row = result["log"]
        review_created = result.get("review") is not None
        # A stored head-impact report outlives a later correction or retry.
        rocked = bool(row.get("rocked")) or request_body.rocked
        if rocked and not review_created:
            logger.error("[sparring] rocked log without review athlete_id=%s", profile.athlete_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Your sparring log was not saved. Please try again.",
            )
        row = _apply_sparring_focus(store, profile.athlete_id, row, request_body)
        return SparringLogResponse(
            log=_sparring_log_record(row),
            review_created=review_created,
            safety_notice=ROCKED_SAFETY_NOTICE if rocked else None,
        )

    @router.get("/api/today/training-focus", response_model=TrainingFocusResponse)
    def get_training_focus(
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> TrainingFocusResponse:
        """The last sparring focus, for the next relevant session. Read with
        health consent only, like the sparring log it comes from."""
        if not evaluate_profile_compliance(profile).health_consent_granted:
            return TrainingFocusResponse()
        return TrainingFocusResponse(**latest_training_focus(
            store, profile.athlete_id, today=resolve_training_day(profile.athlete_timezone)))

    @router.post(
        "/api/today/exercise-log",
        response_model=ExerciseLogResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_exercise_log(
        request_body: ExerciseLogRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> ExerciseLogResponse:
        """Record what was actually done for one block of today's started session.

        The plan is not changed. The block is named by its server-owned
        ``block_id``; the prescription and the training day are resolved here.
        Saving the same block again corrects the earlier log.
        """
        row = record_exercise_log(
            store,
            athlete_id=profile.athlete_id,
            athlete_timezone=profile.athlete_timezone,
            payload=request_body.model_dump(exclude_none=True),
            health_consent_granted=evaluate_profile_compliance(profile).health_consent_granted,
        )
        reconcile_training_xp(store, profile.athlete_id, athlete_timezone=profile.athlete_timezone)
        return ExerciseLogResponse(log=_exercise_log_record(row))

    @router.post(
        "/api/today/exercise-logs",
        response_model=ExerciseLogBatchResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_exercise_logs(
        request_body: ExerciseLogBatchRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> ExerciseLogBatchResponse:
        """Log several blocks of today's started session at once.

        Used when the session timer ends: what the timer recorded is written in
        one request. Every entry passes the single-log rules or nothing is saved.
        """
        rows = record_exercise_logs(
            store,
            athlete_id=profile.athlete_id,
            athlete_timezone=profile.athlete_timezone,
            plan_id=request_body.plan_id,
            entries=[entry.model_dump(exclude_none=True) for entry in request_body.entries],
            health_consent_granted=evaluate_profile_compliance(profile).health_consent_granted,
            keep_existing=request_body.keep_existing,
        )
        reconcile_training_xp(store, profile.athlete_id, athlete_timezone=profile.athlete_timezone)
        return ExerciseLogBatchResponse(logs=[_exercise_log_record(row) for row in rows])

    @router.get("/api/history/exercise-library", response_model=ExerciseLibraryResponse)
    def personal_exercise_library(profile: ProfileRecord = Depends(require_profile),
                                  store: AppStore = Depends(get_store)):
        return exercise_library(store, profile.athlete_id)

    @router.get("/api/history/exercise-performances", response_model=ExercisePerformancesResponse)
    def personal_exercise_performances(exercise: str = Query(min_length=5, max_length=124),
                                       as_of: str | None = Query(default=None, max_length=40),
                                       cursor: str | None = Query(default=None, max_length=512),
                                       limit: int = Query(default=30, ge=1, le=100),
                                       profile: ProfileRecord = Depends(require_profile),
                                       store: AppStore = Depends(get_store)):
        return exercise_performances(store, profile.athlete_id, exercise, as_of=as_of, cursor=cursor, limit=limit,
            health_consent_granted=evaluate_profile_compliance(profile).health_consent_granted)

    @router.get("/api/history/exercises", response_model=ExerciseHistoryResponse)
    def exercise_history(
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> ExerciseHistoryResponse:
        rows = store.list_exercise_history(profile.athlete_id, limit=limit + 1, offset=offset)
        return ExerciseHistoryResponse(
            logs=[_exercise_log_record(row) for row in rows[:limit]],
            next_offset=offset + limit if len(rows) > limit else None,
        )

    @router.get(
        "/api/today/exercise-logs",
        response_model=ExerciseLogListResponse,
    )
    def list_exercise_logs(
        plan_id: UUID = Query(),
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> ExerciseLogListResponse:
        """The athlete's exercise logs for one plan on the current training day."""
        training_day, rows = list_exercise_logs_for_today(
            store,
            athlete_id=profile.athlete_id,
            athlete_timezone=profile.athlete_timezone,
            plan_id=str(plan_id),
        )
        performances, loads = exercise_history_context(store, athlete_id=profile.athlete_id, before_day=training_day)
        return ExerciseLogListResponse(
            training_day=training_day,
            logs=[_exercise_log_record(row) for row in rows],
            recent_performances=[_exercise_log_record(row) for row in performances],
            recent_loads=loads,
        )

    @router.get(
        "/api/today/sparring-logs",
        response_model=SparringLogHistoryResponse,
    )
    def list_sparring_log_history(
        limit: int = Query(default=60, ge=1, le=200),
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> SparringLogHistoryResponse:
        """The athlete's own sparring log (History) plus 7- and 28-day exposure.

        The windows are read separately from the paged list so the totals stay
        complete however few rows the list returns. Days are athlete-local.
        """
        today = date.fromisoformat(resolve_training_day(profile.athlete_timezone))
        logs = [_sparring_log_record(row) for row in store.list_sparring_logs(profile.athlete_id, limit=limit)]
        recent = [
            _sparring_log_record(row)
            for row in store.list_sparring_logs(
                profile.athlete_id,
                limit=500,
                from_day=(today - timedelta(days=27)).isoformat(),
            )
        ]
        return SparringLogHistoryResponse(
            logs=logs,
            current_training_day=today.isoformat(),
            last_7_days=_sparring_window(recent, today=today, days=7),
            last_28_days=_sparring_window(recent, today=today, days=28),
            last_hard_day=store.latest_sparring_log_day(profile.athlete_id, hard=True),
            last_rocked_day=store.latest_sparring_log_day(profile.athlete_id, rocked=True),
        )

    @router.post(
        "/api/today/rehab-responses",
        response_model=RehabResponseResult,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_rehab_responses(
        request_body: RehabResponseRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> RehabResponseResult:
        """Append the injury-specific evidence for one completed rehab session.

        The request returns the immutable injury episode context issued with each
        prompt. The current episode must still match. Drill, side and demand are
        re-resolved from the stored plan and injury record, so nothing the client
        asserts can become evidence.
        """
        require_health_feature_access(profile)
        plan_row = _plan_row_for_completion(
            store, profile=profile, plan_id=request_body.plan_id
        )
        if not plan_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")

        training_day = request_body.training_day or resolve_training_day(
            profile.athlete_timezone
        )
        completion = store.get_session_completion(
            profile.athlete_id, request_body.session_id, training_day
        )
        if not completion:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no session completion to attach this response to",
            )
        if str(completion.get("plan_id") or "") != request_body.plan_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="session completion belongs to another plan",
            )

        saved_contexts = rehab_response_contexts_by_injury(
            completion, athlete_id=profile.athlete_id
        )
        for answer in request_body.answers:
            context = saved_contexts.get(answer.injury_id)
            if context is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="rehab_response_not_pending",
                )
            if str(context.get("injury_episode_id") or "") != str(answer.injury_episode_id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="stale_rehab_response",
                )

        events = record_rehab_exposures(
            store,
            athlete_id=profile.athlete_id,
            plan_row=plan_row,
            training_day=training_day,
            session_id=request_body.session_id,
            completion=completion,
            answers={
                answer.injury_id: {
                    "injury_episode_id": str(answer.injury_episode_id),
                    "during_response": answer.during_response,
                    "limit_response": answer.limit_response,
                }
                for answer in request_body.answers
            },
            expected_contexts=saved_contexts,
        )
        return RehabResponseResult(
            recorded_exposure_ids=[str(event.exposure_id) for event in events],
            recorded_injury_ids=[str(event.injury_id) for event in events],
        )

    return router
