"""GET /api/plans/active reads the active plan's summary columns, not its whole row.

The route only ever returned a summary, but read the full athlete plan row
(~1.1 MB with the planning brief) to build it. Eligibility needs status and
fight_date, and the summary ten columns, so the pointed-to plan is read as its
summary. Every other outcome keeps the full resolution, including
auto-activation and custom-plan checks. Each state below is compared with the
previous logic on an identically seeded store.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from api.contracts.training_day import resolve_training_day_str
from api.plan_mappers import _map_plan_summary
from api.services.active_plan import resolve_active_plan
from support import _build_client

H = {"Authorization": "Bearer athlete-token"}
ATHLETE = "athlete-1"
PLAN_A = "11111111-1111-1111-1111-111111111111"
PLAN_B = "22222222-2222-2222-2222-222222222222"
FOREIGN = "33333333-3333-3333-3333-333333333333"
HEAVY_BRIEF = '{"candidate_pools": "' + "x" * 50_000 + '"}'


def _plan(plan_id: str, *, athlete_id: str = ATHLETE, **fields) -> dict:
    return {
        "id": plan_id, "athlete_id": athlete_id, "status": "ready", "plan_name": f"Camp {plan_id[:2]}",
        "full_name": "Ari Mensah", "technical_style": ["boxing"], "created_at": "2026-06-01T00:00:00+00:00",
        "fight_date": "2099-12-01", "pdf_url": None, "stage2_validator_report": {},
        "planning_brief": HEAVY_BRIEF, "structured_plan": {"weeks": []}, "plan_text": "x" * 5_000,
        **fields,
    }


REVIEW_REPORT = {"warnings": ["check sparring"], "errors": [], "release_decision": "release"}

STATES = {
    "eligible pointer": ({PLAN_A: _plan(PLAN_A)}, PLAN_A),
    "review status derived from the validator report": (
        {PLAN_A: _plan(PLAN_A, status="review_required", stage2_validator_report=REVIEW_REPORT)}, PLAN_A),
    "custom plan id from the validator report": (
        {PLAN_A: _plan(PLAN_A, stage2_validator_report={"custom_plan": {"id": "custom-1"}})}, PLAN_A),
    "open-ended plan": ({PLAN_A: _plan(PLAN_A, fight_date=None)}, PLAN_A),
    "pointer to a finished camp, another plan saved": (
        {PLAN_A: _plan(PLAN_A, fight_date="2020-01-01"), PLAN_B: _plan(PLAN_B)}, PLAN_A),
    "no pointer, one eligible plan (auto-activates)": ({PLAN_A: _plan(PLAN_A)}, None),
    "no pointer, two plans (deliberate choice needed)": ({PLAN_A: _plan(PLAN_A), PLAN_B: _plan(PLAN_B)}, None),
    "pointer to another athlete's plan": (
        {FOREIGN: _plan(FOREIGN, athlete_id="someone-else"), PLAN_A: _plan(PLAN_A)}, FOREIGN),
    "pointer to a held plan": ({PLAN_A: _plan(PLAN_A, status="medical_hold")}, PLAN_A),
}


def _seeded(plans: dict, pointer: str | None):
    client, store, _ = _build_client()
    store.plans.update(copy.deepcopy(plans))
    if pointer:
        store.set_active_plan_id(ATHLETE, pointer)

    def try_set_active_plan_id(athlete_id, plan_id, expected_id, current_training_day=None):
        # Compare-and-set, as the production RPC: only from the expected
        # pointer, and only when this is the athlete's one unarchived plan.
        if store.get_active_plan_id(athlete_id) != expected_id:
            return False
        remaining = [p for p in store.list_user_plans(athlete_id) if p.get("status") != "archived"]
        if len(remaining) != 1 or remaining[0]["id"] != plan_id:
            return False
        store.set_active_plan_id(athlete_id, plan_id)
        return True

    store.try_set_active_plan_id = try_set_active_plan_id
    return client, store


def _previous_response(plans: dict, pointer: str | None):
    """What the route returned before: full-row resolution, then the summary."""
    _, store = _seeded(plans, pointer)
    day = resolve_training_day_str(datetime.now(timezone.utc), athlete_timezone=None)
    row = resolve_active_plan(store, ATHLETE, current_training_day=day).plan
    if not row:
        return 404, None, store.get_active_plan_id(ATHLETE)
    return 200, _map_plan_summary(row, current_training_day=day).model_dump(mode="json"), store.get_active_plan_id(ATHLETE)


@pytest.mark.parametrize("state", list(STATES))
def test_active_plan_response_and_side_effects_are_unchanged(state):
    plans, pointer = STATES[state]
    expected_status, expected_body, expected_pointer = _previous_response(plans, pointer)
    client, store = _seeded(plans, pointer)

    response = client.get("/api/plans/active", headers=H)

    assert response.status_code == expected_status
    if expected_body is not None:
        assert response.json() == expected_body
    # Auto-activation still happens exactly where it did before.
    assert store.get_active_plan_id(ATHLETE) == expected_pointer


def _track_full_reads(store):
    full_reads = {"n": 0}
    in_summary = {"on": False}
    original_full = store.get_plan_for_athlete
    original_summary = store.get_plan_summary_for_athlete

    def full(*args, **kwargs):
        if not in_summary["on"]:
            full_reads["n"] += 1
        return original_full(*args, **kwargs)

    def summary(*args, **kwargs):
        in_summary["on"] = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary["on"] = False

    store.get_plan_for_athlete = full
    store.get_plan_summary_for_athlete = summary
    return full_reads


def test_an_eligible_pointer_never_reads_the_full_plan_row():
    client, store = _seeded(*STATES["eligible pointer"])
    full_reads = _track_full_reads(store)

    response = client.get("/api/plans/active", headers=H)

    assert response.status_code == 200
    assert full_reads["n"] == 0


def test_the_fallback_still_validates_with_the_full_row():
    client, store = _seeded(*STATES["no pointer, one eligible plan (auto-activates)"])
    full_reads = _track_full_reads(store)

    response = client.get("/api/plans/active", headers=H)

    assert response.status_code == 200 and response.json()["plan_id"] == PLAN_A
    assert full_reads["n"] >= 1  # the auto-activation candidate is read whole, as before


def test_a_pointer_read_failure_is_still_a_404_and_writes_nothing():
    client, store = _seeded(*STATES["no pointer, one eligible plan (auto-activates)"])
    writes = {"n": 0}
    original_try_set = store.try_set_active_plan_id
    store.try_set_active_plan_id = lambda *a, **k: (writes.__setitem__("n", writes["n"] + 1), original_try_set(*a, **k))[1]
    store.get_active_plan_id = lambda athlete_id: (_ for _ in ()).throw(RuntimeError("pointer read down"))

    assert client.get("/api/plans/active", headers=H).status_code == 404
    assert writes["n"] == 0
