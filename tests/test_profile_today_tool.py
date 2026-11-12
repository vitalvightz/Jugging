"""tools/profile_today.py stays read-only and never prints athlete content."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

import api.store
from test_today_read_amplification import ATHLETE, _store_with_intake_injury

TOOL = Path(__file__).resolve().parents[1] / "tools" / "profile_today.py"
SECRET_NAME = "Sentinel Camp Name"


def _run(monkeypatch, capsys, store) -> dict:
    monkeypatch.setattr(api.store.SupabaseAppStore, "from_env", classmethod(lambda cls: store))
    monkeypatch.setattr(sys, "argv", ["profile_today.py", "--athlete", ATHLETE,
                                      "--now", "2026-06-03T15:00:00Z", "--warm-runs", "1"])
    runpy.run_path(str(TOOL), run_name="__main__")
    out = capsys.readouterr().out
    assert SECRET_NAME not in out  # content never leaves the server
    return json.loads(out)


def test_reports_stages_store_calls_and_field_sizes(monkeypatch, capsys):
    store = _store_with_intake_injury()
    next(iter(store.plans.values()))["plan_name"] = SECRET_NAME
    report = _run(monkeypatch, capsys, store)

    assert report["response_bytes"] > 0 and report["response_fields"]
    assert "today_core" in report["warm"]["median_stages"]
    assert report["store_calls_one_warm_build"]
    assert report["cold_and_warm_responses_identical"] is True


def test_writes_are_refused_and_reported(monkeypatch, capsys):
    store = _store_with_intake_injury()  # intake injury: the sync would seed a flag
    flags_before = json.dumps(store.injury_flags, sort_keys=True, default=str)
    report = _run(monkeypatch, capsys, store)

    assert report["refused_writes"].get("adopt_or_create_intake_injury_flag")
    assert json.dumps(store.injury_flags, sort_keys=True, default=str) == flags_before
