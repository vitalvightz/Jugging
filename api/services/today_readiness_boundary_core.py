"""Route-level fail-safe boundary for Today readiness and session execution.

This is the thin HTTP-facing boundary. It does NOT invent readiness semantics:
all status/severity/typed-signal decisions are delegated to the single canonical
authority, :mod:`api.services.readiness_failsafe`. The boundary only adds the
route-level protections the pure service cannot express on its own:

* **Check-in** — delegated straight to ``today_service.submit_today_checkin``,
  which already fails closed and emits the canonical typed signal (a failed
  safety read can never persist ``train_as_planned``; unavailable injury/schedule
  context becomes ``pull_back``). No second decision layer here.
* **Session completion** — blocks *current-session execution* (started / done /
  modified) with a retryable ``503`` when active injury state cannot be verified.
* **Injury reconciliation** — refuses to run when open injury flags cannot be
  read, so a failed read never masquerades as an empty injury set (which would
  create duplicates or clear real injuries).
* **Command view** — revokes/softens a STALE stored green recommendation when
  live safety context cannot be verified, using the canonical fail-safe floor.

The public functions mirror ``api.services.today_service`` so the router imports
one module without changing API contracts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

from fastapi import HTTPException, status

from api.contracts.command_view import CommandView, make_risk, sort_risk_watch
from api.contracts.injury_checkin import injury_consequence_tier
from api.contracts.readiness_message import (
    ReadinessAdjustment,
    confidence_band,
    confidence_note,
    context_labels,
    decision_sources,
    safety_checks,
    trigger_labels,
)
from api.services.active_plan import ActivePlanResolution
from api.services import today_service as _today_service
from api.services.today_reads import _BuildReadCache
from api.services.plan_schedule import (
    parse_iso_date,
    resolve_current_week,
    resolve_today_and_next,
)
from api.services.readiness_failsafe import (
    ReadinessContextStatus,
    apply_context_failsafe,
    status_from_components,
)
from api.store import AppStore

logger = logging.getLogger(__name__)

_CURRENT_SESSION_EXECUTION_STATUSES = frozenset({"started", "done", "modified"})

# A neutral "green" adjustment fed to the canonical floor when a STORED
# recommendation reads clean but live context cannot be verified. The floor
# rewrites it to the degraded (modify) or unavailable (pull_back) copy.
_GREEN_BASE_ADJUSTMENT = ReadinessAdjustment(
    decision="train_as_planned",
    title="",
    reason="",
    action="",
)

_COMMAND_VIEW_REMINDER = "Safety context is unavailable, so hard training is not cleared."

# Worst-of ordering for the confidence band. ``None`` (no band, nothing known to
# judge by) ranks lowest so any real band replaces it, and a band is only ever
# lowered, never raised.
_CONFIDENCE_RANK: dict[str | None, int] = {None: 0, "high": 1, "moderate": 2, "low": 3}


@dataclass
class ReadinessContextHealth:
    """Tracks which safety-critical reads failed, by store-method component.

    Status/severity is not decided here — :func:`status_from_components` maps the
    recorded components onto the canonical status vocabulary, so there is exactly
    one place that decides complete / degraded / unavailable.
    """

    failures: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return status_from_components(self.components).status

    @property
    def context_status(self) -> ReadinessContextStatus:
        return status_from_components(self.components)

    @property
    def components(self) -> tuple[str, ...]:
        return tuple(sorted(self.failures))

    def record(self, component: str, exc: BaseException) -> None:
        if component in self.failures:
            return
        self.failures[component] = type(exc).__name__
        logger.error(
            "Readiness context load failed: component=%s error_type=%s",
            component,
            type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        try:
            import sentry_sdk

            with sentry_sdk.push_scope() as scope:
                scope.set_tag("readiness_context_component", component)
                scope.set_tag("readiness_context_status", self.status)
                scope.set_level("error")
                sentry_sdk.capture_exception(exc)
        except Exception:
            # Observability must not become another availability dependency.
            logger.debug("Unable to report readiness context failure to Sentry", exc_info=True)


class _ReadinessTrackingStore:
    """Proxy critical reads, preserving failures instead of converting them to
    healthy-looking empty data."""

    def __init__(
        self,
        store: AppStore,
        health: ReadinessContextHealth,
        *,
        cache_injury_flags: bool = False,
        training_day: str | None = None,
    ):
        self._store = store
        self._training_day = training_day
        self.health = health
        self.last_plan: Mapping[str, Any] | None = None
        self._cache_injury_flags = cache_injury_flags
        self._injury_flags_cache: list[dict[str, Any]] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def _call_list(
        self,
        component: str,
        read: Callable[[], Any],
    ) -> list[dict[str, Any]]:
        try:
            return [dict(row) for row in (read() or [])]
        except Exception as exc:
            self.health.record(component, exc)
            return []

    def list_today_checkins(
        self,
        athlete_id: str,
        *,
        limit: int = 14,
    ) -> list[dict[str, Any]]:
        return self._call_list(
            "recent_checkins", lambda: self._store.list_today_checkins(athlete_id, limit=limit)
        )

    def list_session_completions(
        self,
        athlete_id: str,
        *,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        rows = self._call_list(
            "recent_sessions", lambda: self._store.list_session_completions(athlete_id, limit=limit)
        )
        from fightcamp.completed_workload import has_unverified_completions
        if self._training_day and has_unverified_completions(rows, self._training_day):
            # Expected legacy state, not a failed I/O operation to send to
            # Sentry. It still receives the same incomplete-history floor.
            self.health.failures.setdefault("recent_sessions", "UnverifiedCompletionProvenance")
        return rows

    def list_external_training_sessions(self, athlete_id: str, *, limit: int = 200, include_deleted: bool = False) -> list[dict[str, Any]]:
        return self._call_list("recent_sessions", lambda: self._store.list_external_training_sessions(athlete_id, limit=limit, include_deleted=include_deleted))

    def list_injury_flags(
        self,
        athlete_id: str,
        *,
        statuses: tuple = ("open", "monitoring"),
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if self._cache_injury_flags and self._injury_flags_cache is not None:
            return [dict(row) for row in self._injury_flags_cache[:limit]]

        rows = self._call_list(
            "injury_flags",
            lambda: self._store.list_injury_flags(athlete_id, statuses=statuses, limit=limit),
        )
        if self._cache_injury_flags and not self.health.failures:
            self._injury_flags_cache = [dict(row) for row in rows]
        for row in rows:
            if str(row.get("status") or "") not in {"open", "monitoring"}:
                continue
            if row.get("consequence") not in (None, ""):
                continue
            try:
                injury_consequence_tier(
                    row.get("body_area"),
                    row.get("description"),
                    severity=row.get("severity"),
                )
            except Exception as exc:
                self.health.record("injury_classification", exc)
                break
        return rows

    def get_intake(self, intake_id: str) -> Mapping[str, Any] | None:
        try:
            row = self._store.get_intake(intake_id)
        except Exception as exc:
            self.health.record("intake", exc)
            return None
        if row is None:
            self.health.record(
                "intake",
                LookupError("active plan references an intake row that was not found"),
            )
        return row

    def get_plan_for_athlete(
        self,
        plan_id: str,
        athlete_id: str,
    ) -> Mapping[str, Any] | None:
        # This is the ownership-authority read, not optional readiness context.
        # Let failures abort the request; returning None would misreport an outage
        # as "plan not found" and could bypass the intended ownership distinction.
        row = self._store.get_plan_for_athlete(plan_id, athlete_id)
        if row:
            self.last_plan = row
        return row


def _legacy_plan_has_schedule_source(plan_row: Mapping[str, Any]) -> bool:
    brief = plan_row.get("planning_brief")
    if not isinstance(brief, Mapping):
        return False
    role_map = brief.get("weekly_role_map")
    return isinstance(role_map, Mapping) and bool(role_map.get("weeks"))


def _probe_schedule(
    plan_row: Mapping[str, Any] | None,
    training_day: str,
    health: ReadinessContextHealth,
) -> None:
    """Record parser/resolver failures without treating a legitimate rest day as failure."""

    if not plan_row:
        return

    try:
        structured_weeks = _today_service._structured_plan_weeks(plan_row)
    except Exception as exc:
        health.record("schedule", exc)
        return

    if structured_weeks:
        try:
            # A clean ``None`` is a legitimate rest day. Only parser failure is UNKNOWN.
            _today_service._structured_today_session_entry(plan_row, training_day)
        except Exception as exc:
            health.record("schedule", exc)
        return

    if not _legacy_plan_has_schedule_source(plan_row):
        return

    try:
        training_date = parse_iso_date(training_day)
        if training_date is None:
            raise ValueError("training day could not be parsed")
        _week_index, week = resolve_current_week(plan_row, today=training_date)
        resolve_today_and_next(week, today=training_date)
    except Exception as exc:
        health.record("schedule", exc)


def _status_warning(context_status: ReadinessContextStatus) -> str:
    codes = ", ".join(context_status.reason_codes)
    return f"readiness_context_status={context_status.status}; reason_codes={codes}"


def _raise_execution_unavailable() -> None:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Safety context is temporarily unavailable, so this session cannot be "
            "started or completed. Retry when injury status can be verified."
        ),
        headers={"Retry-After": "30"},
    )


def _injury_flags_readable(store: AppStore, athlete_id: str) -> bool:
    """True when the athlete's open injury flags can be read right now.

    A failed read must NOT be treated as an empty injury set — the caller uses
    this to hard-block (retryable 503) rather than silently proceed on unknown
    injury state.
    """
    health = ReadinessContextHealth()
    try:
        store.list_injury_flags(athlete_id, statuses=("open", "monitoring"))
    except Exception as exc:
        health.record("injury_flags", exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Check-in and landing: the canonical service already fails closed and emits the
# typed signal, so the boundary is a straight pass-through (single authority).
# ---------------------------------------------------------------------------
def submit_today_checkin(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    payload: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a check-in, sharing its pre-write reads like the other Today writes.

    The recommendation is evaluated from the same reads as before; they are
    issued together up front instead of one after another. The save itself is
    unchanged and any write clears the shared reads.
    """
    store = _BuildReadCache(store)
    day = _today_service.resolve_training_day(athlete_timezone, now=now)
    plan_id = str(payload.get("plan_id") or "")
    try:
        store.prefetch([
            ("list_injury_flags", (athlete_id,), {"statuses": ("open", "monitoring"), "limit": 500}),
            ("list_injury_flags", (athlete_id,), {"statuses": ("open", "monitoring")}),
            ("list_session_completions", (athlete_id,), {"limit": 200}),
            ("list_external_training_sessions", (athlete_id,), {"limit": 200}),
            *([("get_training_plan_for_athlete", (plan_id, athlete_id), {})] if plan_id else []),
            ("list_today_checkins_for_day", (athlete_id, day), {}),
            ("latest_sparring_log_day", (athlete_id,), {"rocked": True}),
            ("list_today_checkins", (athlete_id,), {"limit": 4}),
        ])
        flags = store.list_injury_flags(athlete_id, statuses=("open", "monitoring"))
        plan = store.get_training_plan_for_athlete(plan_id, athlete_id) if plan_id else None
        intake_id = str(plan.get("intake_id") or "") if isinstance(plan, Mapping) else ""
        store.prefetch([
            *([("get_intake", (intake_id,), {})] if intake_id else []),
            *[("list_injury_episode_events", (athlete_id,),
               {"injury_id": str(flag["id"]), "injury_episode_id": str(flag["episode_id"])})
              for flag in flags if flag.get("id") and flag.get("episode_id")],
        ])
    except Exception:  # noqa: BLE001 - prefetch is an optimisation; the save reads for itself
        logger.exception("[today] check-in read prefetch failed athlete_id=%s", athlete_id)
    return _today_service.submit_today_checkin(
        store, athlete_id=athlete_id, athlete_timezone=athlete_timezone, payload=payload, now=now
    )


resolve_today_landing = _today_service.resolve_today_landing


def upsert_session_completion(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    payload: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Block current-session execution when active injury state cannot be verified."""

    # Share the preflight and validation reads only inside this mutation.
    # Any write (including intake synchronization) clears mutable inputs.
    store = _BuildReadCache(store)
    completion_status = str(payload.get("status") or "not_started")
    current_day = _today_service.resolve_training_day(athlete_timezone, now=now)
    requested_day = str(payload.get("training_day") or "").strip()
    is_retro_log = bool(requested_day) and requested_day != current_day

    # Retro logs record training that already happened, and skip / not-started do
    # not execute a session, so neither needs a live injury snapshot.
    if completion_status in _CURRENT_SESSION_EXECUTION_STATUSES and not is_retro_log:
        stopped = False
        if completion_status == "modified" and payload.get("rehab_performance") == "stopped":
            existing = store.get_session_completion(athlete_id, str(payload.get("session_id") or ""), current_day) or {}
            stopped = _today_service._is_stopped_started_session(existing, payload)
        if not stopped and not _injury_flags_readable(store, athlete_id):
            _raise_execution_unavailable()

    with _today_service.structured_projection_memo():
        return _today_service.upsert_session_completion(
            store,
            athlete_id=athlete_id,
            athlete_timezone=athlete_timezone,
            payload=payload,
            now=now,
        )


def submit_today_injury_checkin(
    store: AppStore,
    *,
    athlete_id: str,
    payload: Mapping[str, Any],
    athlete_timezone: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile injuries, refusing to run when open flags cannot be read.

    Reconciliation needs the existing open flags for identity. A failed read must
    not masquerade as an empty set (which would create duplicate injuries or clear
    real ones), so this returns a retryable 503 instead. The refreshed readiness
    recommendation is failed-safe by ``today_service`` itself.
    """
    store = _BuildReadCache(store)
    if not _injury_flags_readable(store, athlete_id):
        _raise_execution_unavailable()

    with _today_service.structured_projection_memo():
        return _today_service.submit_today_injury_checkin(
            store,
            athlete_id=athlete_id,
            payload=payload,
            athlete_timezone=athlete_timezone,
            now=now,
        )


def _apply_fail_safe_to_command_view(
    view: CommandView,
    health: ReadinessContextHealth,
) -> CommandView:
    context_status = health.context_status
    if context_status.is_complete:
        return view

    # Structured, machine-readable signals only — never prose the UI must parse.
    warning = _status_warning(context_status)
    if warning not in view.today.warnings:
        view.today.warnings.append(warning)
    for code in context_status.reason_codes:
        token = f"reason_code:{code}"
        if token not in view.today.warnings:
            view.today.warnings.append(token)

    # Only a STALE green recommendation needs revoking; an already-conservative
    # stored state is preserved.
    if str(view.today.recommendation_state) != "train_as_planned":
        # Its own explanation still holds — the signals that made it conservative
        # are still why it is conservative — but the confidence band was computed
        # from the context that STORED it, and this read could not be verified.
        _reband_confidence(view, context_status)
        _hold_unverified_live_prescription(view, context_status)
        return view

    floored = apply_context_failsafe(_GREEN_BASE_ADJUSTMENT, context_status)
    view.today.recommendation_state = floored.decision
    view.today.recommendation_reason = floored.message
    # Map the canonical decision onto the command view's decision tier: degraded
    # softens to MODIFY, unavailable holds at PULL BACK.
    view.today.decision_tier = "pull_back" if floored.decision == "pull_back" else "modify"
    # The explanation has to be revoked with the decision it explained. Left
    # alone, the card would pair a fail-safe hold with the green decision's own
    # contributors and a "high" confidence band — the exact contradiction
    # ("PULL BACK / Confidence: High") this feature exists to prevent.
    _explain_from_triggers(
        view,
        tuple(dict.fromkeys([*context_status.reason_codes, *floored.triggers])),
    )
    reminder = make_risk(
        "reminder",
        text=_COMMAND_VIEW_REMINDER,
        timeframe="active",
    )
    view.risk_watch = sort_risk_watch([*view.risk_watch, reminder])
    _hold_unverified_live_prescription(view, context_status)
    return view


def _hold_unverified_live_prescription(view, context_status):
    if not context_status.is_unavailable or not view.live_prescription:
        return
    live = view.live_prescription
    live["safety_hold"] = True
    live["safety_hold_reason"] = "Current safety context could not be verified. Refresh Today before resuming."
    if not live.get("frozen"):
        from fightcamp.rehab_clinical import content_hash
        live["revision"] = content_hash({key: value for key, value in live.items() if key != "revision"})
    if view.today.decision_tier != "not_checked_in":
        view.today.recommendation_state = "pull_back"
        view.today.decision_tier = "stop"
        view.today.recommendation_reason = "Session blocked\n" + live["safety_hold_reason"] + "\nDo not start or resume this session."


def _explain_from_triggers(view: CommandView, triggers: tuple[str, ...]) -> None:
    """Rebuild the whole explanation from the fail-safe's own trigger codes.

    Status reason codes lead, so the qualifier names the read that actually
    failed rather than the umbrella code.
    """
    view.today.recommendation_trigger_labels = list(trigger_labels(triggers))
    view.today.recommendation_context_labels = list(context_labels(triggers))
    view.today.recommendation_safety_checks = [dict(check) for check in safety_checks(triggers)]
    view.today.recommendation_sources = list(decision_sources(triggers))
    view.today.recommendation_confidence = confidence_band(triggers)
    view.today.recommendation_confidence_note = confidence_note(triggers)


def _reband_confidence(view: CommandView, context_status: ReadinessContextStatus) -> None:
    """Lower a preserved decision's confidence to match THIS read.

    Contributors and sources are left alone: they describe why the stored
    decision was made, and that is unchanged. The band is not, because it reports
    how much could be verified, and right now some of it could not be.

    At an EQUAL band the live failure still replaces the stored qualifier. Two
    different problems can both be "moderate": an athlete with little history,
    and an athlete whose history could not be loaded. Keeping the stored wording
    there told someone with a failed read that they had "no recent days to
    compare", which is both untrue and the wrong remedy — it says check in
    tomorrow, when nothing the athlete does will fix a read that broke.
    """
    banded = confidence_band(context_status.reason_codes)
    if _CONFIDENCE_RANK[banded] < _CONFIDENCE_RANK[view.today.recommendation_confidence]:
        # This read is genuinely better than the one that stored the decision, so
        # the stored band stands: worst-of, never raised.
        return
    view.today.recommendation_confidence = banded
    # A re-check, not a fresh decision: the data was there when the call was made
    # and only the re-read failed, so the qualifier says refresh rather than load.
    # Nothing has to mark the sources as historical any more: the card only shows
    # them at HIGH confidence, and a failed re-read always lands below that.
    view.today.recommendation_confidence_note = confidence_note(
        context_status.reason_codes, re_check=True
    )


def build_today_command_view(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    now: datetime | None = None,
    active_plan: ActivePlanResolution | None = None,
) -> CommandView:
    """Build Today while preserving UNKNOWN safety context as a conservative state.

    ``active_plan`` is a resolution the caller already read for the same
    training day; its row is the one the schedule probe checks.
    """

    health = ReadinessContextHealth()
    tracked = _ReadinessTrackingStore(store, health,
        training_day=_today_service.resolve_training_day(athlete_timezone, now=now))
    if active_plan is not None and active_plan.plan:
        tracked.last_plan = active_plan.plan
    # One scope for the build and the schedule probe, so the probe re-reads the
    # plan card the build already resolved instead of validating it again.
    with _today_service.structured_projection_memo():
        view = _today_service.build_today_command_view(
            tracked,
            athlete_id=athlete_id,
            athlete_timezone=athlete_timezone,
            now=now,
            active_plan=active_plan,
        )
        # Re-check the history that can qualify a stored readiness decision. This
        # safety probe must not depend on unrelated risk-card code happening to read
        # the same rows; otherwise removing that consumer silently disables the
        # degraded-context fail-safe.
        tracked.list_today_checkins(athlete_id)
        _probe_schedule(tracked.last_plan, view.today.training_day, health)
    return _apply_fail_safe_to_command_view(view, health)


__all__ = [
    "ReadinessContextHealth",
    "build_today_command_view",
    "resolve_today_landing",
    "submit_today_checkin",
    "submit_today_injury_checkin",
    "upsert_session_completion",
]
