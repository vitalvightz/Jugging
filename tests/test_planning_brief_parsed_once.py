"""History and the calendar decode a plan's brief text once, with identical results.

History decoded the stored brief (~0.3 MB) once per row, to build a projection
context it discarded; the calendar decoded it four times per request. Each
route now hands its readers the brief parsed once. These tests pin that the
responses are unchanged for a dated camp and an open-ended plan, that no reader
mutates the shared parsed brief, and that undecodable text is left as it was.
"""

from __future__ import annotations

import copy
import json
import sys
from collections import Counter

import pytest

import api.plan_mappers as plan_mappers
import api.routes.plans as plans_routes
import api.routes.today as today_routes
from api.plan_mappers import with_parsed_planning_brief
from api.services.effective_structured_plan import resolve_effective_structured_plan
from api.services.open_plan_timeline import project_open_structured_plan
from support import _build_client
from test_today_read_amplification import PLAN, _store_with_camp
from test_open_plan_programming_fidelity import _reference_row

H = {"Authorization": "Bearer athlete-token"}
HISTORY = "/api/training/history?include_skipped=true&limit=200"
BRIEF_EXTRA = {"profiler_padding_notes": "x" * 20_000}  # a large brief, without changing its meaning


OPEN_PLAN_DAY = "2026-10-20"  # a week into the reference open plan (created 2026-10-13)


def _plan_row(kind: str) -> dict:
    if kind == "open":
        row = {**_reference_row(), "status": "ready", "plan_name": "Open plan", "athlete_id": "athlete-1"}
    else:
        row = copy.deepcopy(_store_with_camp().plans[PLAN])
    brief = row.get("planning_brief") if isinstance(row.get("planning_brief"), dict) else {}
    row["planning_brief"] = json.dumps({**brief, **BRIEF_EXTRA})  # stored as text, as in production
    return row


def _client_with(row: dict):
    client, store, _ = _build_client()
    me = client.get("/api/me", headers=H).json()
    athlete = (me.get("profile") or {}).get("athlete_id") or me["athlete_id"]
    store.plans[PLAN] = {**copy.deepcopy(row), "athlete_id": athlete}
    store.set_active_plan_id(athlete, PLAN)
    structured = resolve_effective_structured_plan(store.plans[PLAN]) or {}
    # An open-ended plan's card is an undated weekly template: date it the way
    # History does, so its sessions can be logged.
    structured, _ = project_open_structured_plan(store.plans[PLAN], structured, current_training_day=OPEN_PLAN_DAY)
    logged = 0
    for week in structured.get("weeks") or []:
        for day in week.get("days") or []:
            for session in day.get("sessions") or []:
                if logged < 6 and session.get("session_id") and day.get("date"):
                    store.upsert_session_completion(athlete, {"plan_id": PLAN, "session_id": session["session_id"],
                                                              "training_day": day["date"], "status": "done"})
                    logged += 1
    return client, logged


def _unparsed(row):
    return dict(row)


@pytest.mark.parametrize("kind", ["dated", "open"])
def test_history_titles_are_unchanged(monkeypatch, kind):
    row = _plan_row(kind)
    client, logged = _client_with(row)
    assert logged, "the fixture must log sessions whose titles come from the plan"
    parsed_once = client.get(HISTORY, headers=H).json()
    monkeypatch.setattr(today_routes, "with_parsed_planning_brief", _unparsed)
    decoded_per_reader = client.get(HISTORY, headers=H).json()

    assert parsed_once == decoded_per_reader
    assert all(session["title"] for session in parsed_once["sessions"])


@pytest.mark.parametrize("kind", ["dated", "open"])
def test_calendar_is_unchanged(monkeypatch, kind):
    client, _ = _client_with(_plan_row(kind))
    parsed_once = client.get(f"/api/plans/{PLAN}/calendar", headers=H)
    monkeypatch.setattr(plans_routes, "with_parsed_planning_brief", _unparsed)
    decoded_per_reader = client.get(f"/api/plans/{PLAN}/calendar", headers=H)

    assert parsed_once.status_code == decoded_per_reader.status_code == 200
    assert parsed_once.json() == decoded_per_reader.json()


@pytest.mark.parametrize("kind", ["dated", "open"])
def test_no_reader_mutates_the_shared_parsed_brief(kind):
    row = with_parsed_planning_brief(_plan_row(kind))
    before = copy.deepcopy(row["planning_brief"])
    structured = resolve_effective_structured_plan(row)
    for day in ("2026-06-03", "2026-06-04", "2026-06-10"):
        today_routes._session_history_title(row, structured or {}, {"training_day": day, "session_id": f"{day}-strength"})
    plan_mappers._map_plan_detail(row, include_admin=False, current_training_day="2026-06-04")

    assert row["planning_brief"] == before


@pytest.mark.parametrize("text", ["", "   ", "not json", "[1, 2]", "\"a string\"", "null"])
def test_text_that_is_not_an_object_is_left_as_it_was(text):
    row = {"id": PLAN, "planning_brief": text}
    assert with_parsed_planning_brief(row) == row


def test_history_decodes_each_plans_brief_once_however_many_rows(monkeypatch):
    row = _plan_row("dated")
    client, logged = _client_with(row)
    brief_text = row["planning_brief"]
    decodes = Counter()
    original = json.loads

    def counting(value, *args, **kwargs):
        # App-code decodes only: the in-memory store emulates the database's
        # planning_brief_runtime by decoding the text itself.
        caller = sys._getframe(1).f_code.co_filename
        if value == brief_text and not caller.endswith("support.py"):
            decodes["brief"] += 1
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", counting)
    client.get(HISTORY, headers=H)

    assert logged >= 3
    assert decodes["brief"] == 1
