"""Source-dosed uphill intervals need exact agreement, not invented timings."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from api.services.custom_plans import source_suggestion
from api.services.training_library import cache


@pytest.mark.parametrize("changes,complete", [
    ({}, True),
    ({"rounds": 5}, False),
    ({"work_sec": 30}, False),
    ({"rest_sec": 120}, False),
    ({"total_minutes": 999}, False),
    ({"rest_sec": None}, False),
    ({"duration": "6x20s uphill, 60s recovery or 30s"}, False),
], ids=["published", "round-conflict", "work-conflict", "rest-conflict", "total-conflict", "missing-rest", "ambiguous-text"])
def test_uphill_source_dose_requires_complete_consistent_evidence(monkeypatch, changes, complete):
    record = deepcopy(next(r for r in cache.get().records if r["entry"]["name"] == "Tempo Hill Runs"))
    identity = record["entry"]["catalogue_id"]
    record["entry"]["raw"].update(changes)
    monkeypatch.setattr(cache, "get", lambda: SimpleNamespace(by_id={identity: record}))
    suggestion = source_suggestion(identity)
    dose = suggestion.dose
    if complete:
        assert dose.model_dump(exclude_none=True) == {
            "mode": "intervals", "rounds": 6, "work_seconds": 20, "rest_seconds": 60, "rpe": 9.0,
        }
    else:
        assert dose.mode == "text"
        assert dose.prescription == suggestion.original
