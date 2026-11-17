"""The camp's fight date is read from the active plan's summary, not its whole row.

Nutrition and a coach's view of a fighter's weight only need the active plan's
fight date, but resolved the plan by reading its full row (~1 MB with the
planning brief). An eligible pointer is now decided from the summary columns,
as GET /api/plans/active does; every other outcome keeps the full resolution.
Each state is compared with the previous logic on an identically seeded store.
"""

from __future__ import annotations

import pytest

from api.contracts.training_day import current_training_day
from api.models import ProfileRecord
from api.plan_mappers import _map_profile_row
from api.routes.nutrition import _active_plan_fight_date
from api.services.active_plan import resolve_active_plan
from test_active_plan_summary_read import ATHLETE, STATES, _seeded, _track_full_reads


def _profile(store) -> ProfileRecord:
    return _map_profile_row(store.get_profile(ATHLETE))


def _previous(plans, pointer):
    _, store = _seeded(plans, pointer)
    profile = _profile(store)
    plan = resolve_active_plan(store, ATHLETE,
                               current_training_day=current_training_day(athlete_timezone=profile.athlete_timezone)).plan
    return (str(plan.get("fight_date") or "") if plan else None), store.get_active_plan_id(ATHLETE)


@pytest.mark.parametrize("state", list(STATES))
def test_the_fight_date_and_auto_activation_are_unchanged(state):
    plans, pointer = STATES[state]
    expected_date, expected_pointer = _previous(plans, pointer)
    _, store = _seeded(plans, pointer)

    assert _active_plan_fight_date(store, _profile(store)) == expected_date
    assert store.get_active_plan_id(ATHLETE) == expected_pointer


def test_an_eligible_pointer_never_reads_the_full_plan_row():
    _, store = _seeded(*STATES["eligible pointer"])
    full_reads = _track_full_reads(store)

    assert _active_plan_fight_date(store, _profile(store))
    assert full_reads["n"] == 0


def test_a_pointer_read_failure_still_gives_no_fight_date():
    _, store = _seeded(*STATES["no pointer, one eligible plan (auto-activates)"])
    profile = _profile(store)
    store.get_active_plan_id = lambda athlete_id: (_ for _ in ()).throw(RuntimeError("pointer read down"))

    assert _active_plan_fight_date(store, profile) is None
