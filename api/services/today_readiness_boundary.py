"""Canonical Today/readiness boundary.

The original fail-safe implementation lives in ``today_readiness_boundary_core``.
This module owns intake-injury synchronization so every caller—HTTP routes,
notifications and XP calculations—uses the same path.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from api import server_timing
from api.contracts.training_day import resolve_training_day_str
from api.store import AppStore
from .active_plan import ActivePlanResolution, resolve_active_plan
from .today_reads import ReadCall, _BuildReadCache
from .today_service import _UPCOMING_COMPLETION_READ_LIMIT
from .intake_injury_sync import (
    _ACTIVE_STATUSES as ACTIVE_FLAG_STATUSES,
    _DEDUPE_STATUSES as DEDUPE_FLAG_STATUSES,
    _intake_injury_candidates,
    _intake_payload_from_row,
    sync_active_plan_intake_injuries,
)
from .today_readiness_boundary_core import (
    ReadinessContextHealth,
    resolve_today_landing,
    submit_today_checkin,
    submit_today_injury_checkin,
    upsert_session_completion,
)
from .today_readiness_boundary_core import build_today_command_view as _build_core

logger = logging.getLogger(__name__)


class _NoLegacyBootstrapStore:
    """Delegate reads but prevent the superseded lazy intake bootstrap writing."""

    def __init__(self, store: AppStore):
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def create_injury_flag(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("legacy intake injury bootstrap is disabled")


_REUSABLE_VIEWS: ContextVar[dict[tuple[Any, ...], tuple[AppStore, Any]] | None] = ContextVar(
    "today_command_view_reuse", default=None
)


@contextmanager
def reuse_today_command_views() -> Iterator[None]:
    """Build each athlete's Today view once per (timezone, instant) in this scope.

    For callers that evaluate the same athlete several times at one fixed
    instant, such as the push sweep, whose dispatchers each used to rebuild the
    full view. Only builds with an explicit ``now`` are reused, and every caller
    gets its own deep copy, so one consumer cannot change what the next reads.
    """
    token = _REUSABLE_VIEWS.set({})
    try:
        yield
    finally:
        _REUSABLE_VIEWS.reset(token)


# The view resolves these again itself: a failed read is retried rather than
# shown as "no plan", and the view's schedule probe checks an unusable plan's row.
_VIEW_RESOLVES_AGAIN = frozenset({"read_failure", "unusable"})


def _resolve_active_plan_once(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    now: datetime,
) -> ActivePlanResolution | None:
    """The active plan, resolved once for the intake sync and the Today view.

    None when resolution raised; each step then resolves and handles the error
    on its own, as before.
    """
    training_day = resolve_training_day_str(now, athlete_timezone=athlete_timezone)
    try:
        return resolve_active_plan(store, athlete_id, current_training_day=training_day)
    except Exception:
        return None


def _begin_athlete_reads(store: _BuildReadCache, *, athlete_id: str, training_day: str):
    """Start, alongside the active-plan resolution, the reads that decide the next wave.

    The latest intake tells whether the intake sync can write, the open flags
    which episodes the view reads, and the day's check-ins whether it
    re-evaluates readiness. Each needs only the athlete and day. Returns the function that waits for them;
    a failed start only loses the overlap, as every consumer reads for itself.
    """
    try:
        return store.begin_prefetch(_sync_flag_reads(athlete_id) + [
            ("get_latest_intake", (athlete_id,), {}),
            ("list_today_checkins_for_day", (athlete_id, training_day), {}),
        ])
    except Exception:  # noqa: BLE001 - prefetch is an optimisation
        logger.exception("[today] athlete read prefetch failed athlete_id=%s", athlete_id)
        return lambda: None


def _sync_flag_reads(athlete_id: str) -> list[ReadCall]:
    return [
        ("list_injury_flags", (athlete_id,), {"statuses": ACTIVE_FLAG_STATUSES, "limit": 500}),
        ("list_injury_flags", (athlete_id,), {"statuses": DEDUPE_FLAG_STATUSES, "limit": 500}),
    ]


def _view_plan_id(active_plan: ActivePlanResolution | None) -> str:
    plan = active_plan.plan if active_plan is not None and active_plan.source not in _VIEW_RESOLVES_AGAIN else None
    return str(plan.get("id") or "") if isinstance(plan, dict) else ""


def _sync_cannot_write(store: _BuildReadCache, *, athlete_id: str, active_plan: ActivePlanResolution | None) -> bool:
    """True only when the intake sync provably writes nothing this build.

    The sync writes only for injuries its intake lists. Its intake is the
    plan's intake row, or the latest intake when the plan names none; the
    latest row read already is that row when the ids match (both ``select *``
    of the same table, and Today never writes an intake). Anything else,
    including a failed read, counts as a possible write.
    """
    try:
        if active_plan is None:
            return False  # the sync resolves the plan itself
        plan = active_plan.plan
        if not isinstance(plan, dict):
            return plan is None  # no plan: the sync only lists flags
        plan_id = str(plan.get("id") or "").strip()
        intake_id = str(plan.get("intake_id") or "").strip()
        latest = store.get_latest_intake(athlete_id)
        if not plan_id or (intake_id and str((latest or {}).get("id") or "") != intake_id):
            return False
        return not _intake_injury_candidates(_intake_payload_from_row(latest), plan_id=plan_id)
    except Exception:  # noqa: BLE001 - only decides what to prefetch
        return False


def _prefetch_sync_reads(
    store: _BuildReadCache,
    *,
    athlete_id: str,
    training_day: str,
    active_plan: ActivePlanResolution | None,
) -> None:
    """Issue the intake sync's reads together instead of one after another.

    The sync reads the open flags, the plan's intake row and the
    open+resolved flags strictly in sequence, though each depends only on the
    athlete and the resolved plan: three round trips before the view's reads
    could start. Read here concurrently, they reach the sync from this
    operation's cache exactly as if it had read them itself. A failed read is
    not kept, so the sync reads it again and handles its own failure.

    When the sync provably writes nothing, the same wave also carries every
    read the view will make: its rows are then exactly what the view would
    read after the sync. When it may write, those reads wait for the view's
    own prefetch, after the write, as before.
    """
    plan = active_plan.plan if active_plan is not None else None
    intake_id = str(plan.get("intake_id") or "").strip() if isinstance(plan, dict) else ""
    reads: list[ReadCall] = _sync_flag_reads(athlete_id)
    if intake_id:
        reads.append(("get_intake", (intake_id,), {}))
    if _sync_cannot_write(store, athlete_id=athlete_id, active_plan=active_plan):
        reads += _view_first_wave(athlete_id, training_day, _view_plan_id(active_plan))
        reads += _episode_reads(store, athlete_id)
        if _any_checkin_on_day(store, athlete_id=athlete_id, day=training_day):
            reads += _readiness_decision_reads(athlete_id)
    store.prefetch(reads)


def _view_first_wave(athlete_id: str, day: str, plan_id: str) -> list[ReadCall]:
    """The view's reads that need only the athlete, the day and the plan."""
    return [
        ("list_injury_flags", (athlete_id,), {"statuses": ("open", "monitoring"), "limit": 500}),
        ("list_injury_flags", (athlete_id,), {"statuses": ("open", "monitoring", "resolved"), "limit": 500}),
        ("get_today_checkin", (athlete_id, plan_id or None, day), {}),
        ("list_today_checkins_for_day", (athlete_id, day), {}),
        ("list_session_completions_from_day", (athlete_id, day), {"limit": _UPCOMING_COMPLETION_READ_LIMIT + 1}),
        ("list_today_checkins", (athlete_id,), {"limit": 14}),
        ("list_session_completions", (athlete_id,), {"limit": 14}),
        ("list_session_completions", (athlete_id,), {"limit": 30}),
        ("get_profile", (athlete_id,), {}),
        ("get_latest_intake", (athlete_id,), {}),
        ("list_rehab_schedule_completions", (athlete_id,),
         {"from_day": (datetime.fromisoformat(day) - timedelta(days=14)).date().isoformat()}),
        ("list_sparring_logs", (athlete_id,), {"limit": 50, "from_day": day}),
        ("list_pending_delayed_rehab", (athlete_id, day), {}),
    ]


def _episode_reads(store: _BuildReadCache, athlete_id: str) -> list[ReadCall]:
    """Each open injury's episode history and rehab exposures."""
    flags = store.list_injury_flags(athlete_id, statuses=("open", "monitoring"), limit=500)
    episodes = [(str(f["id"]), str(f["episode_id"])) for f in flags if f.get("id") and f.get("episode_id")]
    return [
        (name, (athlete_id,), {"injury_id": injury, "injury_episode_id": episode})
        for injury, episode in episodes
        for name in ("list_injury_episode_events", "list_rehab_exposures")
    ]


def _prefetch_view_reads(
    store: _BuildReadCache,
    *,
    athlete_id: str,
    training_day: str,
    active_plan: ActivePlanResolution | None,
) -> None:
    """Read what the view will ask for in two concurrent waves.

    Called after the intake sync, whose writes clear the operation's cache, so
    every prefetched row postdates them. Without a write the sync's wave
    already holds them and nothing is read again. The view then reads exactly
    as before; identical reads are answered from these results. A key the view
    no longer uses only costs one wasted read; a read the view adds is simply
    sequential.
    """
    plan_id = _view_plan_id(active_plan)
    day = training_day
    store.prefetch(_view_first_wave(athlete_id, day, plan_id))
    # Second wave: each open injury's episode history and rehab exposures.
    second = _episode_reads(store, athlete_id)
    if _has_checkin_today(store, athlete_id=athlete_id, plan_id=plan_id, day=day):
        second += _readiness_decision_reads(athlete_id)
    store.prefetch(second)


def _any_checkin_on_day(store: _BuildReadCache, *, athlete_id: str, day: str) -> bool:
    """Whether the athlete checked in on ``day`` for any plan.

    Covers every row ``_has_checkin_today`` can find, so the readiness reads
    are never left to the view's sequential path when it will need them; a
    check-in for another plan only costs reads the view does not use. If the
    read failed, assume yes.
    """
    try:
        return bool(store.list_today_checkins_for_day(athlete_id, day))
    except Exception:  # noqa: BLE001 - only decides what to prefetch
        return True


def _has_checkin_today(store: _BuildReadCache, *, athlete_id: str, plan_id: str, day: str) -> bool:
    """Whether the view will re-evaluate the readiness decision (a check-in today).

    Answered from the first wave's read. If that read failed, assume yes: a
    needless concurrent read is cheaper than four sequential ones.
    """
    try:
        return bool(store.get_today_checkin(athlete_id, plan_id or None, day))
    except Exception:  # noqa: BLE001 - only decides what to prefetch
        return True


def _readiness_decision_reads(athlete_id: str) -> list[ReadCall]:
    """The reads the view's readiness decision makes one after another.

    With a check-in today the view re-evaluates the decision live
    (``_readiness_context_and_status``):
    the latest head-impact sparring day, recent check-ins and both training
    history sources, each a sequential round trip. Keys match the view's calls
    exactly, so they are answered from this wave instead. A medical hold
    without a check-in also re-evaluates; those builds keep reading them in
    sequence rather than prefetching on every open injury.
    """
    return [
        ("latest_sparring_log_day", (athlete_id,), {"rocked": True}),
        ("list_today_checkins", (athlete_id,), {"limit": 4}),
        ("list_session_completions", (athlete_id,), {"limit": 200}),
        ("list_external_training_sessions", (athlete_id,), {"limit": 200, "include_deleted": False}),
    ]


def build_today_command_view(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    now: datetime | None = None,
):
    """Synchronize intake injuries before every canonical Today build."""
    views = _REUSABLE_VIEWS.get() if now is not None else None
    key = (id(store), athlete_id, athlete_timezone, now)
    if views is not None:
        cached = views.get(key)
        if cached is not None and cached[0] is store:
            return cached[1].model_copy(deep=True)
    # One instant for the whole build, so the sync and the view agree on the
    # training day the shared plan resolution was made for.
    now = now or datetime.now(timezone.utc)
    build_store = store
    store = _BuildReadCache(store)
    training_day = resolve_training_day_str(now, athlete_timezone=athlete_timezone)
    with server_timing.measure("today_resolve"):
        # The reads that decide the next wave need only the athlete and day,
        # so they run while the plan's pointer and row are read in sequence.
        finish_athlete_reads = _begin_athlete_reads(store, athlete_id=athlete_id, training_day=training_day)
        active_plan = _resolve_active_plan_once(
            store, athlete_id=athlete_id, athlete_timezone=athlete_timezone, now=now
        )
        finish_athlete_reads()
    with server_timing.measure("today_sync"):
        try:
            _prefetch_sync_reads(store, athlete_id=athlete_id, training_day=training_day, active_plan=active_plan)
        except Exception:  # noqa: BLE001 - prefetch is an optimisation; the sync reads for itself
            logger.exception("[today] sync read prefetch failed athlete_id=%s", athlete_id)
        sync_active_plan_intake_injuries(
            store,
            athlete_id=athlete_id,
            athlete_timezone=athlete_timezone,
            now=now,
            active_plan=active_plan,
        )
    with server_timing.measure("today_reads"):
        try:
            _prefetch_view_reads(
                store,
                athlete_id=athlete_id,
                training_day=training_day,
                active_plan=active_plan,
            )
        except Exception:  # noqa: BLE001 - prefetch is an optimisation; the view reads for itself
            logger.exception("[today] read prefetch failed athlete_id=%s", athlete_id)
    with server_timing.measure("today_core"):
        view = _build_core(
            _NoLegacyBootstrapStore(store),
            athlete_id=athlete_id,
            athlete_timezone=athlete_timezone,
            now=now,
            active_plan=(
                None if active_plan is None or active_plan.source in _VIEW_RESOLVES_AGAIN else active_plan
            ),
        )
    if views is not None:
        views[key] = (build_store, view.model_copy(deep=True))
    return view


__all__ = [
    "ReadinessContextHealth",
    "build_today_command_view",
    "resolve_today_landing",
    "reuse_today_command_views",
    "submit_today_checkin",
    "submit_today_injury_checkin",
    "upsert_session_completion",
]
