"""Short-lived reuse of the Today command view for read-only consumers.

``GET /api/today`` always builds a fresh command view. Every Today refresh in
the web app is immediately followed by an XP progress refresh, which used to
rebuild the whole command view (active plan, check-in, completions, schedule,
injuries, rehab state) a second time just to derive its "next action" list.

``/api/today`` now remembers the view it just built; XP progress reuses it when
it is recent and was built for the same athlete and timezone, and builds its own
otherwise. Any mutating request by the athlete drops the entry (see the
``invalidate_athlete_read_caches`` middleware in ``api.app``), so a check-in or
completion is never answered from a pre-write view. Only derived read models
use this; Today itself is never served from it.

On app start the web app requests Today and XP progress together, so XP used
to find nothing remembered and build a second full view alongside Today's. A
read-only consumer may now wait for the athlete's Today build that is already
running and reuse it. A write during that build moves the athlete's
generation on, so the build's view is neither remembered nor handed to anyone
waiting: they build their own.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from threading import Event, Lock
from typing import Any, Callable, Iterator

from api.store import AppStore

DEFAULT_TODAY_COMMAND_CACHE_TTL_SECONDS = 30.0
_MAX_ENTRIES = 2000
_REGISTRY_ATTR = "_unlxck_today_command_cache"
_STATE_ATTR = "_unlxck_today_command_builds"
_LOCK = Lock()
# Longer than any healthy build; a waiter that gives up just builds its own.
_INFLIGHT_WAIT_SECONDS = 15.0


def _ttl_seconds() -> float:
    raw_value = os.getenv("TODAY_COMMAND_CACHE_TTL_SECONDS")
    if raw_value is None or not raw_value.strip():
        return DEFAULT_TODAY_COMMAND_CACHE_TTL_SECONDS
    try:
        parsed = float(raw_value.strip())
    except ValueError:
        return DEFAULT_TODAY_COMMAND_CACHE_TTL_SECONDS
    return parsed if parsed >= 0 else DEFAULT_TODAY_COMMAND_CACHE_TTL_SECONDS


def _registry(store: AppStore) -> dict[str, tuple[str, Any, float]] | None:
    registry = getattr(store, _REGISTRY_ATTR, None)
    if isinstance(registry, dict):
        return registry
    with _LOCK:
        registry = getattr(store, _REGISTRY_ATTR, None)
        if isinstance(registry, dict):
            return registry
        registry = {}
        try:
            setattr(store, _REGISTRY_ATTR, registry)
        except Exception:  # noqa: BLE001 - an uncacheable store just rebuilds
            return None
        return registry


def _build_state(store: AppStore) -> dict[str, Any] | None:
    """Per-store in-flight builds and athlete write generations."""
    state = getattr(store, _STATE_ATTR, None)
    if isinstance(state, dict):
        return state
    with _LOCK:
        state = getattr(store, _STATE_ATTR, None)
        if isinstance(state, dict):
            return state
        state = {"generation": {}, "inflight": {}}
        try:
            setattr(store, _STATE_ATTR, state)
        except Exception:  # noqa: BLE001 - an unannotatable store just never shares builds
            return None
        return state


@contextmanager
def today_command_build(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
) -> Iterator[Callable[[Any], None]]:
    """Announce a fresh Today build; ``publish(view)`` remembers it.

    The view is only remembered (and handed to waiters) when no write by the
    athlete happened since the build began.
    """
    state = _build_state(store) if athlete_id and _ttl_seconds() > 0 else None
    if state is None:
        yield lambda view: None
        return
    done = Event()
    with _LOCK:
        generation = state["generation"].get(athlete_id, 0)
        entry = {"timezone": str(athlete_timezone or ""), "event": done}
        state["inflight"][athlete_id] = entry

    def publish(view: Any) -> None:
        ttl = _ttl_seconds()
        registry = _registry(store)
        if ttl <= 0 or registry is None or view is None:
            return
        # Check and insert as one step: a save committing in between would
        # otherwise leave this pre-save view for XP to reuse for the whole TTL.
        with _LOCK:
            if state["generation"].get(athlete_id, 0) == generation:
                _insert_locked(registry, athlete_id, athlete_timezone, view, ttl)

    try:
        yield publish
    finally:
        with _LOCK:
            if state["inflight"].get(athlete_id) is entry:
                state["inflight"].pop(athlete_id, None)
        done.set()


def await_today_command(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
) -> Any | None:
    """A recent view, else the one an in-flight build for this athlete publishes."""
    recent = recent_today_command(store, athlete_id=athlete_id, athlete_timezone=athlete_timezone)
    if recent is not None or not athlete_id:
        return recent
    state = getattr(store, _STATE_ATTR, None)
    if not isinstance(state, dict):
        return None
    with _LOCK:
        entry = state["inflight"].get(athlete_id)
    if entry is None or entry["timezone"] != str(athlete_timezone or ""):
        # A build may have finished between the two lookups.
        return recent_today_command(store, athlete_id=athlete_id, athlete_timezone=athlete_timezone)
    if not entry["event"].wait(_INFLIGHT_WAIT_SECONDS):
        return None
    return recent_today_command(store, athlete_id=athlete_id, athlete_timezone=athlete_timezone)


def remember_today_command(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    view: Any,
) -> None:
    ttl = _ttl_seconds()
    if ttl <= 0 or not athlete_id or view is None:
        return
    registry = _registry(store)
    if registry is None:
        return
    with _LOCK:
        _insert_locked(registry, athlete_id, athlete_timezone, view, ttl)


def _insert_locked(
    registry: dict[str, tuple[str, Any, float]],
    athlete_id: str,
    athlete_timezone: str | None,
    view: Any,
    ttl: float,
) -> None:
    """Store a view; the caller holds ``_LOCK``."""
    now = time.monotonic()
    if athlete_id not in registry and len(registry) >= _MAX_ENTRIES:
        for key in [key for key, entry in registry.items() if entry[2] <= now]:
            registry.pop(key, None)
        if len(registry) >= _MAX_ENTRIES:
            registry.clear()
    registry[athlete_id] = (str(athlete_timezone or ""), view, now + ttl)


def recent_today_command(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
) -> Any | None:
    if not athlete_id or _ttl_seconds() <= 0:
        return None
    registry = _registry(store)
    if registry is None:
        return None
    with _LOCK:
        entry = registry.get(athlete_id)
        if entry is None:
            return None
        timezone_key, view, expires_at = entry
        if expires_at <= time.monotonic():
            registry.pop(athlete_id, None)
            return None
        if timezone_key != str(athlete_timezone or ""):
            return None
        return view


def forget_today_command(store: AppStore, athlete_id: str | None) -> None:
    if not athlete_id:
        return
    state = getattr(store, _STATE_ATTR, None)
    registry = getattr(store, _REGISTRY_ATTR, None)
    with _LOCK:
        if isinstance(state, dict):
            # Builds that began before this write must not be remembered.
            state["generation"][athlete_id] = state["generation"].get(athlete_id, 0) + 1
        if isinstance(registry, dict):
            registry.pop(athlete_id, None)


__all__ = [
    "await_today_command",
    "forget_today_command",
    "recent_today_command",
    "remember_today_command",
    "today_command_build",
]
