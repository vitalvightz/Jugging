"""Share post-save plan/history reads while reconciling one completion."""

from copy import deepcopy
from typing import Any

from api.store import TRAINING_PLAN_SELECT, AppStore

_TRAINING_PLAN_COLUMNS = tuple(TRAINING_PLAN_SELECT.split(","))


class CompletionReads:
    """Created after persistence and discarded at the end of the request.

    Ownership and history limits remain part of each exact read key. Failed
    reads are retried by the next consumer, and returned rows cannot mutate the
    shared copy. Only streak/XP writes are known to leave these inputs intact;
    other mutations invalidate them before delegating, including failed writes.
    Training-engagement reconciliation writes only XP and leaves plan/history
    inputs intact. Active-plan pointers and reward state stay uncached.
    """

    def __init__(self, store: AppStore):
        self._store = store
        self._reads: dict[tuple[Any, ...], Any] = {}

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_store", "_reads"}:
            object.__setattr__(self, name, value)
        else:
            # Preserve existing store-instance capability caches (XP rollout).
            setattr(self._store, name, value)

    def get_training_plan_for_athlete(self, plan_id: str, athlete_id: str) -> Any:
        # A full row this request already read answers the projection too.
        full = self._reads.get(("get_plan_for_athlete", (plan_id, athlete_id), ()))
        if isinstance(full, dict):
            return {column: deepcopy(full[column]) for column in _TRAINING_PLAN_COLUMNS if column in full}
        return self.__getattr__("get_training_plan_for_athlete")(plan_id, athlete_id)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._store, name)
        if callable(attr) and name in {"get_plan_for_athlete", "get_training_plan_for_athlete", "list_plan_session_completions"}:
            def read(*args: Any, **kwargs: Any) -> Any:
                key = (name, args, tuple(sorted(kwargs.items())))
                if key not in self._reads:
                    self._reads[key] = deepcopy(attr(*args, **kwargs))
                return deepcopy(self._reads[key])
            return read
        if (not callable(attr) or name.startswith(("get_", "list_"))
                or name in {"upsert_athlete_streaks", "award_xp", "validate_xp_abuse_hardening",
                            "reconcile_training_engagement_xp"}):
            return attr

        def write(*args: Any, **kwargs: Any) -> Any:
            self._reads.clear()
            return attr(*args, **kwargs)

        return write
