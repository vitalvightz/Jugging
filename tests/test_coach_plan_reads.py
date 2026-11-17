"""Coach plan screens read each plan input once and only the columns they use.

- A plan list read the fighter's active-plan pointer once per answered row,
  the same round trip each time; it is now read once per list.
- A fighter's planned sessions read the whole active plan row (~1 MB with the
  planning brief) to project its calendar. An eligible pointer now reads the
  runtime brief, as the plan calendar does; every other outcome keeps the
  full resolution, including auto-activation.

Responses are compared with the previous logic on identically seeded stores.
"""

from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

import api.routes.coach as coach_routes
from api.services.coach_plans import assignment_view, assignment_views
from test_coach_fighter_access import AS_COACH, BASE, FIGHTER, camp  # noqa: F401 - fixture
from test_open_plan_programming_fidelity import _reference_row
from test_today_read_amplification import PLAN, _store_with_camp

BRIEF_EXTRA = {"candidate_pools": {"padding": "x" * 20_000}}  # dropped by the runtime brief


def _plan_row(kind: str) -> dict:
    if kind == "open":
        row = {**_reference_row(), "status": "ready", "plan_name": "Open plan"}
    else:
        row = copy.deepcopy(_store_with_camp().plans[PLAN])
    brief = row.get("planning_brief") if isinstance(row.get("planning_brief"), dict) else {}
    row["planning_brief"] = json.dumps({**brief, **BRIEF_EXTRA})  # stored as text, as in production
    return {**row, "id": PLAN, "athlete_id": FIGHTER}


def _seed(store, kind: str, pointer: bool, **changes):
    store.plans[PLAN] = {**_plan_row(kind), **changes}
    if pointer:
        store.set_active_plan_id(FIGHTER, PLAN)


def _full_row_logic(monkeypatch):
    # The previous reader: the active plan read whole, for every outcome.
    monkeypatch.setattr(coach_routes, "_RuntimePlanReads", lambda store: store)


STATES = {
    "eligible dated camp": ("dated", True, {"fight_date": "2099-12-01"}),
    "eligible open plan": ("open", True, {}),
    "no pointer, one plan (auto-activates)": ("dated", False, {}),
    "pointer to a finished camp": ("dated", True, {"fight_date": "2020-01-01"}),
}


@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("include_past", [False, True])
def test_planned_sessions_are_unchanged(camp, monkeypatch, state, include_past):  # noqa: F811
    kind, pointer, changes = STATES[state]
    path = BASE + "/training/plan" + ("?include_past=true" if include_past else "")
    client, store, _ = camp
    _seed(store, kind, pointer, **changes)
    with monkeypatch.context() as patch:
        _full_row_logic(patch)
        expected = client.get(path, headers=AS_COACH)
        expected_pointer = store.get_active_plan_id(FIGHTER)
    if not pointer:
        store.set_active_plan_id(FIGHTER, None)  # replay from the same starting state

    actual = client.get(path, headers=AS_COACH)

    assert actual.status_code == expected.status_code == 200
    assert actual.json() == expected.json()
    assert store.get_active_plan_id(FIGHTER) == expected_pointer
    if state.startswith("eligible") and include_past:
        assert actual.json(), "the fixture must have planned sessions"


def test_an_eligible_pointer_never_reads_the_full_plan_row(camp):  # noqa: F811
    client, store, _ = camp
    _seed(store, "dated", True, fight_date="2099-12-01")
    full_reads = Counter()
    in_runtime = {"on": False}
    original_full, original_runtime = store.get_plan_for_athlete, store.get_plan_runtime_for_athlete

    def full(*args, **kwargs):
        if not in_runtime["on"]:  # the in-memory store derives its runtime row from the full one
            full_reads["full"] += 1
        return original_full(*args, **kwargs)

    def runtime(*args, **kwargs):
        in_runtime["on"] = True
        try:
            return original_runtime(*args, **kwargs)
        finally:
            in_runtime["on"] = False

    store.get_plan_for_athlete, store.get_plan_runtime_for_athlete = full, runtime

    assert client.get(BASE + "/training/plan?include_past=true", headers=AS_COACH).json()
    assert full_reads["full"] == 0


class _PointerStore:
    def __init__(self, active):
        self.active = active
        self.reads = 0

    def get_active_plan_id(self, fighter_id):
        self.reads += 1
        return self.active


def test_a_plan_list_reads_the_fighters_pointer_once_with_the_same_statuses():
    rows = [
        {"id": "draft", "fighter_id": FIGHTER},
        {"id": "pending", "fighter_id": FIGHTER, "assigned_at": "2026-11-01"},
        {"id": "declined", "fighter_id": FIGHTER, "assigned_at": "2026-11-01", "declined_at": "2026-11-02"},
        {"id": "active", "fighter_id": FIGHTER, "accepted_at": "2026-11-02", "published_plan_id": "plan-a"},
        {"id": "superseded", "fighter_id": FIGHTER, "accepted_at": "2026-10-02", "published_plan_id": "plan-b"},
        {"id": "accepted-without-plan", "fighter_id": FIGHTER, "accepted_at": "2026-10-01"},
    ]
    before = _PointerStore("plan-a")
    expected = [assignment_view(before, row) for row in rows]
    after = _PointerStore("plan-a")

    assert assignment_views(after, rows) == expected
    assert [view["assignment_status"] for view in expected] == [
        "draft", "pending", "declined", "active", "superseded", "superseded"]
    assert before.reads == 3 and after.reads == 1


def test_a_list_without_answered_plans_reads_no_pointer():
    store = _PointerStore("plan-a")
    assert [v["assignment_status"] for v in assignment_views(store, [{"id": "d", "fighter_id": FIGHTER}])] == ["draft"]
    assert store.reads == 0
