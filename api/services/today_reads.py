"""Request-local reads for Today builds and mutations.

Writes clear mutable inputs before delegation, including failed writes. This
scope never reuses a readiness decision or a read from an earlier request.
"""

from copy import deepcopy
from typing import Any

from api.store import PROFILE_COMPLIANCE_SELECT, AppStore

_COMPLIANCE_COLUMNS = tuple(column.strip() for column in PROFILE_COMPLIANCE_SELECT.split(","))


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
        if callable(attr) and name in {"get_latest_intake", "list_today_checkins", "list_session_completions", "list_external_training_sessions",
            "get_training_plan_for_athlete", "get_session_completion", "get_today_checkin", "get_profile"}:
            def read(*args: Any, **kwargs: Any) -> Any:
                key = (name, args, tuple(sorted(kwargs.items())))
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
