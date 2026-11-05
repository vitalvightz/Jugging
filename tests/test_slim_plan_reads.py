"""Athlete plan reads leave out what they never use, with identical results.

- Runtime reads (Today, XP) get planning_brief without the planner's
  candidate_pools via the planning_brief_runtime computed field.
- Athlete whole-plan reads (plan viewer, active-plan resolution) leave out the
  admin-only Stage-2 blobs. Plan detail keeps candidate_pools: it stamps
  exercise identity from them.
"""

from __future__ import annotations

import json
from copy import deepcopy

import support
from api.plan_mappers import _map_plan_detail
from api.services.today_readiness_boundary import build_today_command_view
from api.services.xp_progress import build_xp_progress
from api.store import ADMIN_ONLY_PLAN_COLUMNS, ATHLETE_PLAN_SELECT
from test_exercise_identity import _blocks, _plan
from test_today_read_amplification import ATHLETE, NOW, PLAN, _store_with_intake_injury

POOLS = {
    "GPP": {
        "strength_slots": [{"selected": {"name": "Box Jump (Max Height)"}, "alternates": []}],
        "conditioning_slots": [{"selected": {"name": "Tempo Shadowboxing"}}],
    }
}


def _detail_row() -> dict:
    return {
        "id": PLAN, "athlete_id": ATHLETE, "status": "ready", "plan_name": "Camp",
        "created_at": "2026-06-01T00:00:00+00:00", "fight_date": "2026-12-01",
        "structured_plan": _plan([
            {"display_name": "Box Jump (Max Height)", "block_type": "plyometric_power"},
            {"display_name": "Tempo Shadowboxing - 3 x 3 min", "block_type": "conditioning"},
        ]),
        "planning_brief": json.dumps({"fight_date": "2026-12-01", "candidate_pools": POOLS}),
        "stage2_payload": {"prompt": "x" * 1000}, "stage2_handoff_text": "handoff", "stage2_retry_text": "retry",
    }


def _keys(detail) -> list:
    return [b.get("exercise_key") for b in _blocks(detail.model_dump(mode="json")["outputs"]["structured_plan"])]


def test_athlete_plan_detail_is_identical_without_admin_only_blobs():
    full = _detail_row()
    athlete = {k: v for k, v in full.items() if k in ATHLETE_PLAN_SELECT.split(",")}
    assert not set(ADMIN_ONLY_PLAN_COLUMNS) & set(athlete)

    expected = _map_plan_detail(deepcopy(full), include_admin=False, current_training_day="2026-11-05")
    actual = _map_plan_detail(athlete, include_admin=False, current_training_day="2026-11-05")

    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
    assert _keys(actual) == ["box-jump-max-height", "tempo-shadowboxing"]


def test_plan_detail_still_needs_candidate_pools_so_it_keeps_the_full_brief():
    row = _detail_row()
    stripped = {**row, "planning_brief": support.planning_brief_runtime(row["planning_brief"])}
    with_pools = _map_plan_detail(deepcopy(row), include_admin=False, current_training_day="2026-11-05")
    without = _map_plan_detail(stripped, include_admin=False, current_training_day="2026-11-05")
    # The runtime projection must not be used for plan detail.
    assert _keys(with_pools) != _keys(without)


def _with_pools(store):
    brief = json.loads(store.plans[PLAN].get("planning_brief") or "{}") if isinstance(
        store.plans[PLAN].get("planning_brief"), str) else dict(store.plans[PLAN].get("planning_brief") or {})
    store.plans[PLAN]["planning_brief"] = json.dumps({**brief, "candidate_pools": POOLS})
    return store


def test_today_and_xp_are_identical_without_candidate_pools(monkeypatch):
    slim = _with_pools(_store_with_intake_injury())
    full = slim  # same rows, ids and timestamps; repeat builds are idempotent here
    view_slim = build_today_command_view(slim, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    xp_slim = build_xp_progress(slim, athlete_id=ATHLETE, athlete_timezone="UTC", profile=slim.get_profile(ATHLETE))

    # Baseline: the training projection hands over the whole brief, pools included.
    monkeypatch.setattr(support, "planning_brief_runtime", lambda brief: brief)
    assert "candidate_pools" in full.get_training_plan_for_athlete(PLAN, ATHLETE)["planning_brief"]
    view_full = build_today_command_view(full, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    xp_full = build_xp_progress(full, athlete_id=ATHLETE, athlete_timezone="UTC", profile=full.get_profile(ATHLETE))

    assert view_slim.model_dump(mode="json") == view_full.model_dump(mode="json")
    assert xp_slim == xp_full


def test_in_memory_runtime_brief_matches_the_sql_function():
    brief = {"fight_date": "2026-12-01", "weekly_role_map": {"weeks": []}, "candidate_pools": POOLS}
    assert json.loads(support.planning_brief_runtime(json.dumps(brief))) == {
        "fight_date": "2026-12-01", "weekly_role_map": {"weeks": []}}
    unchanged = json.dumps({"fight_date": "2026-12-01"})
    assert support.planning_brief_runtime(unchanged) is unchanged  # byte-identical without pools
    assert support.planning_brief_runtime("not json") == "not json"
    assert support.planning_brief_runtime(None) is None
