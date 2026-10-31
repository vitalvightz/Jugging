"""Authoritative active-plan resolution.

``profiles.active_plan_id`` is the only active-plan pointer. The referenced plan
is returned only when it belongs to the athlete, has an athlete-displayable
status and has not passed its fight date in the athlete-local training day. When
no valid active pointer remains, the sole non-archived plan is automatically
activated only if eligible; multiple saved plans always require a deliberate
choice. Read failures never trigger a fallback. Overlapping saved or draft
plans are allowed, but activating an overlapping second plan requires an
explicit pause or replacement choice. ``pause`` preserves the previous plan row
unchanged and switches the single active pointer to the selected plan, allowing
the previous plan to be reactivated later. ``replace`` switches the pointer and
archives the previous plan. Eligible statuses are ``ready`` and
``publishable_with_flags``. Generated, review, hold, failed, archived, missing,
and deleted plans are never active. Overview and Today both call this resolver;
UI code must not guess with “latest visible plan” logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import json
import logging
from typing import Any, Literal, Mapping, Protocol

from fastapi import HTTPException, status
from api.datetimes import parse_calendar_date as _parse_date

logger = logging.getLogger(__name__)

ELIGIBLE_ACTIVE_PLAN_STATUSES = {"ready", "publishable_with_flags"}
ACTIVE_PLAN_OVERLAP_ACTIONS = {"pause", "replace"}
ACTIVE_PLAN_OVERLAP_CONFLICT_MESSAGE = (
    "This overlaps with your current active plan. Do you want to replace the current plan, pause it, or choose a new start date?"
)
ACTIVE_PLAN_OVERLAP_CONFLICT_CODE = "active_plan_overlap"
ACTIVE_PLAN_REPLACE_FAILED_MESSAGE = "Unable to replace the current active plan. The original active plan was restored."
PLAN_HAS_ENDED_CODE = "plan_has_ended"
PLAN_HAS_ENDED_MESSAGE = "This fight camp has ended and cannot be activated."
PlanActivationState = Literal["eligible", "fight_date_passed", "status_ineligible"]
NEVER_ACTIVE_PLAN_STATUSES = {
    "generated",
    "review_required",
    "held_for_review",
    "triage_blocked",
    "medical_hold",
    "restricted_rehab_only",
    "needs_review",
    "failed",
    "archived",
}


class ActivePlanStore(Protocol):
    def list_user_plans(self, athlete_id: str) -> list[dict[str, Any]]: ...
    def get_plan_for_athlete(self, plan_id: str, athlete_id: str) -> dict[str, Any] | None: ...
    def get_active_plan_id(self, athlete_id: str) -> str | None: ...
    def set_active_plan_id(self, athlete_id: str, plan_id: str) -> None: ...
    def try_set_active_plan_id(self, athlete_id: str, plan_id: str, expected_id: str | None) -> bool: ...
    def archive_plan_for_athlete(self, plan_id: str, athlete_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ActivePlanResolution:
    plan: dict[str, Any] | None
    source: str

    @property
    def plan_id(self) -> str | None:
        return str(self.plan.get("id")) if self.plan and self.plan.get("id") else None


@dataclass(frozen=True)
class PlanDateRange:
    start: date
    end: date


def normalize_plan_status(row: dict[str, Any] | None) -> str:
    return str((row or {}).get("status") or "").strip().lower()


def is_active_plan_eligible(
    row: dict[str, Any] | None,
    *,
    current_training_day: date | str | None = None,
) -> bool:
    return get_plan_activation_state(
        row,
        current_training_day=current_training_day,
    ) == "eligible"


def _explicit_active_plan_id(store: ActivePlanStore, athlete_id: str) -> str | None:
    value = store.get_active_plan_id(athlete_id)
    return str(value).strip() or None if value is not None else None


def _decode_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


def get_plan_activation_state(
    row: dict[str, Any] | None,
    *,
    current_training_day: date | str | None = None,
) -> PlanActivationState:
    """Return the server-authoritative activation state for one saved plan.

    A scheduled fight camp remains eligible throughout its athlete-local fight
    day. Blank fight dates are ongoing plans. Non-empty malformed legacy dates
    fail closed so they can remain viewable without becoming operational.
    """

    if normalize_plan_status(row) not in ELIGIBLE_ACTIVE_PLAN_STATUSES:
        return "status_ineligible"

    raw_fight_date = (row or {}).get("fight_date")
    fight_date_text = str(raw_fight_date or "").strip()
    if not fight_date_text:
        return "eligible"

    fight_date = _parse_date(fight_date_text)
    if fight_date is None:
        return "status_ineligible"

    training_day = _parse_date(current_training_day) if current_training_day is not None else date.today()
    if training_day is None:
        training_day = date.today()
    if fight_date < training_day:
        return "fight_date_passed"
    return "eligible"


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _collect_structured_dates(row: dict[str, Any]) -> list[date]:
    structured_plan = _decode_mapping(row.get("structured_plan"))
    weeks = structured_plan.get("weeks")
    if not isinstance(weeks, list):
        return []
    dates: list[date] = []
    for week in weeks:
        if not isinstance(week, Mapping):
            continue
        for key in ("start_date", "end_date"):
            parsed = _parse_date(week.get(key))
            if parsed:
                dates.append(parsed)
        days = week.get("days")
        if isinstance(days, list):
            for day in days:
                if isinstance(day, Mapping):
                    parsed = _parse_date(day.get("date"))
                    if parsed:
                        dates.append(parsed)
    return dates


def _collect_planning_brief_dates(row: dict[str, Any]) -> list[date]:
    planning_brief = _decode_mapping(row.get("planning_brief"))
    role_map = planning_brief.get("weekly_role_map")
    weeks = role_map.get("weeks") if isinstance(role_map, Mapping) else None
    if not isinstance(weeks, list):
        return []
    dates: list[date] = []
    for week in weeks:
        if not isinstance(week, Mapping):
            continue
        calendar_days = week.get("calendar_days")
        if isinstance(calendar_days, list):
            for day in calendar_days:
                if isinstance(day, Mapping):
                    parsed = _parse_date(day.get("calendar_date"))
                    if parsed:
                        dates.append(parsed)
    return dates


def _camp_length_weeks(row: dict[str, Any]) -> int | None:
    planning_brief = _decode_mapping(row.get("planning_brief"))
    for container in (
        planning_brief.get("fight_demands"),
        planning_brief.get("athlete_snapshot"),
        planning_brief.get("athlete_model"),
    ):
        if isinstance(container, Mapping):
            parsed = _int_or_none(container.get("camp_length_weeks"))
            if parsed:
                return parsed
    return None


def plan_date_range(row: dict[str, Any] | None) -> PlanDateRange | None:
    if not row:
        return None

    dates = _collect_structured_dates(row) or _collect_planning_brief_dates(row)
    if dates:
        return PlanDateRange(start=min(dates), end=max(dates))

    fight_date = _parse_date(row.get("fight_date"))
    camp_length_weeks = _camp_length_weeks(row)
    if fight_date and camp_length_weeks:
        return PlanDateRange(
            start=fight_date - timedelta(days=(camp_length_weeks * 7) - 1),
            end=fight_date,
        )

    return None


def _ranges_overlap(left: PlanDateRange, right: PlanDateRange) -> bool:
    return left.start <= right.end and right.start <= left.end


def _active_plan_overlap(
    store: ActivePlanStore,
    athlete_id: str,
    candidate: dict[str, Any],
    *,
    current_training_day: date | str | None,
) -> dict[str, Any] | None:
    current = resolve_active_plan(
        store,
        athlete_id,
        current_training_day=current_training_day,
        auto_activate=False,
    ).plan
    if not current or str(current.get("id") or "") == str(candidate.get("id") or ""):
        return None
    if not is_active_plan_eligible(current, current_training_day=current_training_day):
        return None

    # The resolver already returned this owned row with both timeline inputs.
    # No writes occur between resolution and overlap evaluation.
    current_range = plan_date_range(current)
    candidate_range = plan_date_range(candidate)
    from api.services.open_plan_timeline import open_plan_spec
    if open_plan_spec(current) or open_plan_spec(candidate):
        # Renewable plans have no end date; their undated templates must still
        # participate in the existing explicit pause/replace choice.
        return current
    if not current_range or not candidate_range:
        return None
    return current if _ranges_overlap(current_range, candidate_range) else None


def _activate_only_remaining_plan(
    store: ActivePlanStore,
    athlete_id: str,
    *,
    expected_id: str | None,
    original_source: str,
    current_training_day: date | str | None,
) -> ActivePlanResolution:
    unchanged = ActivePlanResolution(plan=None, source=original_source)
    # Older test stores have no conditional-update capability. Real stores use
    # compare-and-set so a simultaneous manual selection is never overwritten.
    if not hasattr(store, "try_set_active_plan_id"):
        return unchanged
    try:
        remaining = [
            row for row in store.list_user_plans(athlete_id)
            if normalize_plan_status(row) != "archived"
        ]
        if len(remaining) != 1 or not is_active_plan_eligible(
            remaining[0], current_training_day=current_training_day
        ):
            return unchanged
        candidate_id = str(remaining[0].get("id") or "")
        candidate = store.get_plan_for_athlete(candidate_id, athlete_id)
        if not candidate or not is_active_plan_eligible(candidate, current_training_day=current_training_day):
            return unchanged
        if store.try_set_active_plan_id(athlete_id, candidate_id, expected_id):
            return ActivePlanResolution(plan=candidate, source="auto_single")
        # Another request updated the pointer first. Respect that choice.
        return resolve_active_plan(store, athlete_id, current_training_day=current_training_day, auto_activate=False)
    except Exception:  # noqa: BLE001 - a failed repair must never break a plan read
        logger.exception("[active-plan] auto_activation_failed athlete_id=%s", athlete_id)
        return unchanged


def resolve_active_plan(
    store: ActivePlanStore,
    athlete_id: str,
    *,
    current_training_day: date | str | None = None,
    auto_activate: bool = True,
) -> ActivePlanResolution:
    try:
        explicit_id = _explicit_active_plan_id(store, athlete_id)
    except Exception:  # noqa: BLE001 - authority reads must fail closed
        logger.exception("[active-plan] pointer_read_failed athlete_id=%s", athlete_id)
        return ActivePlanResolution(plan=None, source="read_failure")
    if not explicit_id:
        return (
            _activate_only_remaining_plan(
                store, athlete_id, expected_id=None, original_source="none",
                current_training_day=current_training_day,
            ) if auto_activate else ActivePlanResolution(plan=None, source="none")
        )

    try:
        explicit = store.get_plan_for_athlete(explicit_id, athlete_id)
    except Exception:  # noqa: BLE001 - authority reads must fail closed
        logger.exception(
            "[active-plan] referenced_plan_read_failed athlete_id=%s plan_id=%s",
            athlete_id,
            explicit_id,
        )
        return ActivePlanResolution(plan=None, source="read_failure")
    if explicit is None:
        logger.warning(
            "[active-plan] referenced_plan_missing_or_not_owned athlete_id=%s plan_id=%s",
            athlete_id,
            explicit_id,
        )
        source = "invalid_reference"
    elif not is_active_plan_eligible(explicit, current_training_day=current_training_day):
        logger.warning(
            "[active-plan] referenced_plan_unusable athlete_id=%s plan_id=%s status=%s",
            athlete_id,
            explicit_id,
            normalize_plan_status(explicit),
        )
        source = "unusable"
    else:
        return ActivePlanResolution(plan=explicit, source="explicit")
    return (
        _activate_only_remaining_plan(
            store, athlete_id, expected_id=explicit_id, original_source=source,
            current_training_day=current_training_day,
        ) if auto_activate else ActivePlanResolution(plan=None, source=source)
    )

def set_active_plan(
    store: ActivePlanStore,
    athlete_id: str,
    plan_id: str,
    *,
    overlap_action: str | None = None,
    current_training_day: date | str | None = None,
) -> dict[str, Any]:
    plan = store.get_plan_for_athlete(plan_id, athlete_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    if _decode_mapping(plan.get("planning_brief")).get("custom_plan"):
        from api.structured_plan_models import safe_parse_structured_plan
        from api.services.open_plan_timeline import project_open_structured_plan
        parsed = safe_parse_structured_plan(plan.get("structured_plan"))
        if not parsed.ok or parsed.plan is None:
            raise HTTPException(409, "This custom plan's prescription is invalid. Edit the plan before activating.")
        _, context = project_open_structured_plan(
            plan, parsed.plan.model_dump(mode="json"), current_training_day=current_training_day or date.today()
        )
        if context["projection_status"] != "projected":
            raise HTTPException(409, "This custom plan's schedule is invalid. Edit the plan before activating.")
    activation_state = get_plan_activation_state(
        plan,
        current_training_day=current_training_day,
    )
    if activation_state == "fight_date_passed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": PLAN_HAS_ENDED_CODE,
                "message": PLAN_HAS_ENDED_MESSAGE,
                "activation_state": activation_state,
            },
        )
    if activation_state != "eligible":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="plan is not eligible to become active")
    normalized_action = str(overlap_action or "").strip().lower() or None
    if normalized_action is not None and normalized_action not in ACTIVE_PLAN_OVERLAP_ACTIONS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid overlap action")
    overlapping_active = _active_plan_overlap(
        store,
        athlete_id,
        plan,
        current_training_day=current_training_day,
    )
    if overlapping_active:
        previous_custom = _decode_mapping(overlapping_active.get("planning_brief")).get("custom_plan") or {}
        candidate_custom = _decode_mapping(plan.get("planning_brief")).get("custom_plan") or {}
        if previous_custom.get("id") and previous_custom.get("id") == candidate_custom.get("id"):
            day = str(current_training_day or date.today())[:10]
            rows = store.list_session_completions(athlete_id, limit=500)
            if any(str(row.get("plan_id")) == str(overlapping_active["id"])
                   and str(row.get("training_day"))[:10] == day
                   and row.get("status") in {"started", "done", "modified", "skipped"} for row in rows):
                raise HTTPException(409, "Today's workout has already been logged or started. Activate your edited plan on the next training day.")
    if overlapping_active and normalized_action is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": ACTIVE_PLAN_OVERLAP_CONFLICT_CODE,
                "message": ACTIVE_PLAN_OVERLAP_CONFLICT_MESSAGE,
            },
        )
    if overlapping_active and normalized_action == "pause":
        # The active pointer is the pause state. Preserve the previous plan row
        # unchanged so it remains available for deliberate reactivation later.
        store.set_active_plan_id(athlete_id, plan_id)
        return plan
    if overlapping_active and normalized_action == "replace":
        current_plan_id = str(overlapping_active.get("id") or "")
        store.set_active_plan_id(athlete_id, plan_id)
        try:
            store.archive_plan_for_athlete(current_plan_id, athlete_id)
        except Exception as exc:
            store.set_active_plan_id(athlete_id, current_plan_id)
            if isinstance(exc, HTTPException):
                raise HTTPException(
                    status_code=exc.status_code,
                    detail=ACTIVE_PLAN_REPLACE_FAILED_MESSAGE,
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=ACTIVE_PLAN_REPLACE_FAILED_MESSAGE,
            ) from exc
        return plan
    store.set_active_plan_id(athlete_id, plan_id)
    return plan
