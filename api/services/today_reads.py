"""Request-local reads for Today builds and mutations.

Writes clear mutable inputs before delegation, including failed writes. This
scope never reuses a readiness decision or a read from an earlier request.
"""

import contextvars
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any, Iterable

from api.store import PROFILE_COMPLIANCE_SELECT, AppStore

_COMPLIANCE_COLUMNS = tuple(column.strip() for column in PROFILE_COMPLIANCE_SELECT.split(","))

# Pure reads one Today operation may repeat. Each is one Data API round trip.
_SHARED_READS = frozenset({
    "get_latest_intake", "list_today_checkins", "list_session_completions", "list_external_training_sessions",
    "get_training_plan_for_athlete", "get_session_completion", "get_today_checkin", "get_profile",
    "list_today_checkins_for_day", "list_session_completions_from_day", "list_injury_episode_events",
    "list_rehab_exposures", "list_rehab_schedule_completions", "list_sparring_logs", "list_pending_delayed_rehab",
    "latest_sparring_log_day",
})

# Leaf I/O only: a task never waits on another task, so a busy pool queues
# rather than deadlocks. Shared by every request in the process.
_PREFETCH_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="today-read")

ReadCall = tuple[str, tuple[Any, ...], dict[str, Any]]


class _BuildReadCache:
    """Successful reads shared within one Today operation, never across requests.

    The intake-injury sync and the view each read the plan's intake row and the
    athlete's injury flags. Within one build those reads return the same rows,
    so the second is served from the first. Only successful reads are kept: a
    failed read is repeated by the next caller, so the view's readiness tracking
    still sees (and reports) its own failure. Any other store call may write an
    injury flag, so it clears cached flags and history. History keys include the
    requested limit; a smaller window never stands in for a larger one. Today
    never writes an intake row. Every caller gets its own copy of the rows.
    """

    def __init__(self, store: AppStore):
        self._store = store
        self._intakes: dict[str, Any] = {}
        self._flags: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        self._reads: dict[tuple[Any, ...], Any] = {}

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._store, name)
        if callable(attr) and name in _SHARED_READS:
            def read(*args: Any, **kwargs: Any) -> Any:
                key = _key(name, args, kwargs)
                if key not in self._reads:
                    # Failed reads are never cached; the next safety consumer
                    # still performs its own read and records any failure.
                    self._reads[key] = deepcopy(attr(*args, **kwargs))
                return deepcopy(self._reads[key])
            return read
        if not callable(attr) or name.startswith(("get_", "list_")):
            return attr

        def write(*args: Any, **kwargs: Any) -> Any:
            self._flags.clear()
            self._reads.clear()
            return attr(*args, **kwargs)

        return write

    def get_plan_for_athlete(self, plan_id: str, athlete_id: str) -> Any:
        return self.get_training_plan_for_athlete(plan_id, athlete_id)

    def get_profile_compliance(self, athlete_id: str) -> Any:
        # The full profile row read earlier in this operation already carries
        # every consent column; project it instead of another round trip.
        # Writes clear the shared reads, so consent is never older than the
        # operation's own last write.
        row = self._reads.get(("get_profile", (athlete_id,), ()))
        if isinstance(row, dict) and all(column in row for column in _COMPLIANCE_COLUMNS):
            return {column: deepcopy(row[column]) for column in _COMPLIANCE_COLUMNS}
        return self._store.get_profile_compliance(athlete_id)

    def get_intake(self, intake_id: str) -> Any:
        if intake_id not in self._intakes:
            self._intakes[intake_id] = self._store.get_intake(intake_id)
        return deepcopy(self._intakes[intake_id])

    def list_injury_flags(self, athlete_id: str, *, statuses: tuple = ("open", "monitoring"), limit: int = 20) -> Any:
        key = (athlete_id, tuple(statuses), limit)
        if key not in self._flags:
            self._flags[key] = [dict(row) for row in (self._store.list_injury_flags(athlete_id, statuses=statuses, limit=limit) or [])]
        return deepcopy(self._flags[key])

    def prefetch(self, calls: Iterable[ReadCall]) -> None:
        """Issue independent reads together instead of one after another.

        A Today build's reads are mostly known once the active plan, training
        day and open injuries are: issuing them in one concurrent wave turns
        ~20 sequential Data API round trips into about one. Each result lands in
        this operation's cache exactly as if the consumer had read it first, so
        a later identical read is served from memory. A failed read is not kept:
        its consumer reads again and records its own failure. Reads not in the
        shared set, or already held, are skipped.
        """
        pending: list[tuple[str, tuple[Any, ...], Any]] = []
        for name, args, kwargs in calls:
            if name == "list_injury_flags":
                key = (args[0], tuple(kwargs.get("statuses", ("open", "monitoring"))), kwargs.get("limit", 20))
                if key in self._flags:
                    continue
            elif name == "get_intake":
                if args[0] in self._intakes:
                    continue
            elif name not in _SHARED_READS or _key(name, args, kwargs) in self._reads:
                continue
            reader = getattr(self._store, name, None)
            if not callable(reader):
                continue
            context = contextvars.copy_context()
            pending.append((name, (args, kwargs), _PREFETCH_POOL.submit(context.run, reader, *args, **kwargs)))
        for name, (args, kwargs), future in pending:
            try:
                value = future.result()
            except Exception:  # noqa: BLE001 - the consumer repeats the read and handles it
                continue
            if name == "list_injury_flags":
                key = (args[0], tuple(kwargs.get("statuses", ("open", "monitoring"))), kwargs.get("limit", 20))
                self._flags.setdefault(key, [dict(row) for row in (value or [])])
            elif name == "get_intake":
                self._intakes.setdefault(args[0], value)
            else:
                self._reads.setdefault(_key(name, args, kwargs), deepcopy(value))


def _key(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, ...]:
    return (name, args, tuple(sorted(kwargs.items())))
